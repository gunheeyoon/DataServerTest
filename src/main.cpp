// Use a common socket handle and close operation on Windows and POSIX.
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <bcrypt.h>
using Socket = SOCKET;
constexpr Socket badSocket = INVALID_SOCKET;
void closeSocket(Socket s) { closesocket(s); }
#else
#include <arpa/inet.h>
#include <cerrno>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/select.h>
#include <sys/random.h>
#include <unistd.h>
using Socket = int;
constexpr Socket badSocket = -1;
void closeSocket(Socket s) { close(s); }
#endif
#include <algorithm>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <locale>
#include <map>
#include <mutex>
#include <optional>
#include <regex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <variant>
#include <vector>

// The catalog is immutable after startup. State contains only live field values.
using FieldValue = std::variant<double, int, bool, std::string>;
using State = std::map<std::string, FieldValue>;
struct Field {
    std::string type;
    bool streamable = false;
    bool readable = false;
    bool writable = false;
    std::vector<std::string> choices;
};
std::map<std::string, Field> catalog;
std::vector<std::string> names;
State state;
std::mutex stateMutex;

bool parseBool(const std::string& value) {
    if (value == "true") return true;
    if (value == "false") return false;
    throw std::runtime_error("expected_true_or_false");
}

FieldValue parseValue(const std::string& name, const Field& field, const std::string& text) {
    if (field.type == "double") {
        std::istringstream input(text);
        input.imbue(std::locale::classic());
        double number;
        if (!(input >> number) || !input.eof() || !std::isfinite(number))
            throw std::runtime_error("invalid_double");
        return number;
    }
    if (field.type == "int") {
        // Keep support for an explicit positive sign in TCP SET commands.
        const char* first = text.data();
        const char* end = first + text.size();
        if (first != end && *first == '+' && first + 1 != end &&
            first[1] >= '0' && first[1] <= '9') ++first;
        int number;
        const auto parsed = std::from_chars(first, end, number);
        if (parsed.ec != std::errc{} || parsed.ptr != end)
            throw std::runtime_error("invalid_integer");
        return number;
    }
    if (field.type == "bool") return parseBool(text);
    if (std::find(field.choices.begin(), field.choices.end(), text) == field.choices.end())
        throw std::runtime_error(name == "weather" ? "invalid_weather" : "invalid_enum_value");
    return text;
}

std::string value(const State& values, const std::string& name) {
    const auto& field = values.at(name);
    if (const auto* text = std::get_if<std::string>(&field)) return *text;
    if (const auto* flag = std::get_if<bool>(&field)) return *flag ? "true" : "false";
    if (const auto* number = std::get_if<int>(&field)) return std::to_string(*number);
    // Keep double serialization independent of the machine's locale.
    std::ostringstream out;
    out.imbue(std::locale::classic());
    out << std::setprecision(17) << std::get<double>(field);
    return out.str();
}

// Preserve the original date validation when the catalog declares these integers.
void validate(const State& values) {
    const auto integer = [&values](const char* name) -> const int* {
        const auto field = values.find(name);
        return field == values.end() ? nullptr : std::get_if<int>(&field->second);
    };
    const auto outside = [](const int* number, int minimum, int maximum) {
        return number && (*number < minimum || *number > maximum);
    };
    const auto* year = integer("year");
    const auto* month = integer("month");
    const auto* day = integer("day");
    if (outside(year, 1, 9999) || outside(month, 1, 12) ||
        outside(integer("hour"), 0, 23) || outside(integer("second"), 0, 59))
        throw std::runtime_error("invalid_date_or_time");

    int maximumDay = 31;
    if (year && month) {
        const int days[] = {31,28,31,30,31,30,31,31,30,31,30,31};
        const bool leap = *year % 4 == 0 && (*year % 100 != 0 || *year % 400 == 0);
        maximumDay = days[*month - 1] + (*month == 2 && leap ? 1 : 0);
    }
    if (outside(day, 1, maximumDay)) throw std::runtime_error("invalid_day");
}
std::vector<std::string> tokens(const std::string& line) {
    std::istringstream in(line); std::vector<std::string> result;
    for(std::string t;in>>t;) result.push_back(t);
    return result;
}
using StreamDefinitions = std::map<std::string, std::vector<std::string>>;
StreamDefinitions streamDefinitions;

// Read the JSON shapes used by the field catalog and group configuration.
class ConfigReader {
public:
    explicit ConfigReader(std::istream& input) : input(input) {}

    void readCatalog() {
        expect('{');
        if (!consume('}')) {
            do {
                const auto name = identifier();
                if (catalog.count(name)) fail("duplicate field " + name);
                expect(':');
                try {
                    readField(name);
                } catch (const std::runtime_error& error) {
                    fail(name + ": " + error.what());
                }
            } while (consume(','));
            expect('}');
        }
        finish();
        validate(state);
    }

    StreamDefinitions readGroups() {
        StreamDefinitions definitions;
        expect('{');
        if (!consume('}')) {
            do {
                const auto name = identifier();
                if (definitions.count(name)) fail("duplicate stream " + name);
                auto& fields = definitions[name];
                expect(':');
                fields = identifierList();
                for (const auto& field : fields) {
                    if (!catalog.count(field)) fail(name + " references unknown field " + field);
                }
            } while (consume(','));
            expect('}');
        }
        finish();
        return definitions;
    }

private:
    std::istream& input;

    [[noreturn]] static void fail(const std::string& detail) {
        throw std::runtime_error(detail);
    }

    struct Number { std::string text; };
    using Scalar = std::variant<Number, bool, std::string>;

    void readField(const std::string& name) {
        Field field;
        std::optional<Scalar> initial;
        std::set<std::string> properties;
        expect('{');
        if (!consume('}')) {
            do {
                const auto key = identifier();
                if (!properties.insert(key).second) fail("duplicate property " + key);
                expect(':');
                if (key == "type") field.type = identifier();
                else if (key == "default") initial = scalar();
                else if (key == "readable") field.readable = boolean();
                else if (key == "writable") field.writable = boolean();
                else if (key == "streamable") field.streamable = boolean();
                else if (key == "values") field.choices = identifierList();
                else fail("unknown property " + key);
            } while (consume(','));
            expect('}');
        }
        if (!initial) fail("missing default");
        if (field.type == "double" || field.type == "int") {
            const auto* number = std::get_if<Number>(&*initial);
            if (!number) fail("expected numeric default");
            state.emplace(name, parseValue(name, field, number->text));
        } else if (field.type == "bool") {
            const auto* flag = std::get_if<bool>(&*initial);
            if (!flag) fail("expected boolean default");
            state.emplace(name, *flag);
        } else if (field.type == "enum") {
            const auto* text = std::get_if<std::string>(&*initial);
            if (!text || field.choices.empty()) fail("expected enum values and a string default");
            state.emplace(name, parseValue(name, field, *text));
        } else {
            fail("expected type double, int, bool or enum");
        }
        if (field.type != "enum" && !field.choices.empty()) fail("values requires type enum");
        catalog.emplace(name, field);
        names.push_back(name);
    }

    std::vector<std::string> identifierList() {
        std::vector<std::string> items;
        expect('[');
        do {
            const auto item = identifier();
            if (std::find(items.begin(), items.end(), item) != items.end())
                fail("duplicate list entry " + item);
            items.push_back(item);
        } while (consume(','));
        expect(']');
        return items;
    }

    bool boolean() {
        skipWhitespace();
        const bool flag = input.peek() == 't';
        for (const char* ch = flag ? "true" : "false"; *ch; ++ch) {
            if (input.get() != *ch) fail("expected JSON boolean");
        }
        return flag;
    }

    Scalar scalar() {
        skipWhitespace();
        if (input.peek() == '"') return identifier();
        if (input.peek() == 't' || input.peek() == 'f') return boolean();
        std::string number;
        for (int ch = input.peek(); (ch >= '0' && ch <= '9') || ch == '-' ||
             ch == '+' || ch == '.' || ch == 'e' || ch == 'E'; ch = input.peek()) {
            if (number.size() == 128) fail("numeric default is too long");
            number.push_back(static_cast<char>(input.get()));
        }
        static const std::regex syntax(R"(-?(0|[1-9][0-9]*)([.][0-9]+)?([eE][+-]?[0-9]+)?)");
        if (!std::regex_match(number, syntax)) fail("expected JSON scalar default");
        return Number{number};
    }

    void finish() {
        skipWhitespace();
        if (input.peek() != std::char_traits<char>::eof() || input.bad())
            fail("unexpected data after the object");
    }

    void skipWhitespace() {
        while (true) {
            const int ch = input.peek();
            if (ch != ' ' && ch != '\t' && ch != '\r' && ch != '\n') return;
            input.get();
        }
    }

    bool consume(char expected) {
        skipWhitespace();
        if (input.peek() != expected) return false;
        input.get();
        return true;
    }

    void expect(char expected) {
        if (!consume(expected)) fail(std::string("expected '") + expected + "'");
    }

    std::string identifier() {
        expect('"');
        std::string name;
        for (int ch = input.get(); ch != '"'; ch = input.get()) {
            // JSON can also spell an ASCII identifier character as a Unicode escape.
            if (ch == '\\' && input.get() == 'u') {
                ch = 0;
                for (int i = 0; i < 4; ++i) {
                    const int digit = input.get();
                    if (digit >= '0' && digit <= '9') ch = ch * 16 + digit - '0';
                    else if (digit >= 'a' && digit <= 'f') ch = ch * 16 + digit - 'a' + 10;
                    else if (digit >= 'A' && digit <= 'F') ch = ch * 16 + digit - 'A' + 10;
                    else fail("invalid Unicode escape");
                }
            }
            const bool letter = (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z');
            if (!letter && !(ch >= '0' && ch <= '9') && ch != '_' && ch != '-')
                fail("expected an identifier containing only ASCII letters, digits, _ or -");
            if (name.size() == 64) fail("identifier exceeds 64 characters");
            name.push_back(static_cast<char>(ch));
        }
        if (name.empty()) fail("identifier cannot be empty");
        return name;
    }
};

struct Subscription {
    unsigned short port = 0;
    unsigned long long sequence = 0;
};
using Subscriptions = std::map<std::string, Subscription>;

struct Publication {
    using Clock = std::chrono::steady_clock;
    static constexpr auto window = std::chrono::seconds(10);

    Socket owner;
    std::uint32_t peerAddress;
    StreamDefinitions::const_iterator group;
    std::uint64_t lastSequence = 0;
    Clock::time_point windowEnd = Clock::now() + window;
    std::uint64_t accepted = 0;
    std::optional<std::uint64_t> previousAccepted = std::nullopt;

    // Advance even when no datagrams arrive, so idle windows report zero.
    void advanceWindow(Clock::time_point now) {
        if (now < windowEnd) return;
        const auto windows = (now - windowEnd) / window + 1;
        previousAccepted = windows == 1 ? accepted : 0;
        accepted = 0;
        windowEnd += windows * window;
    }
};

// Ownership, sequence checks and state changes share the same mutex.
std::map<std::string, Publication> publications;
constexpr int maxSampleBytes = 4096;

std::string publicationToken() {
    unsigned char bytes[16];
#ifdef _WIN32
    if (BCryptGenRandom(nullptr, bytes, sizeof(bytes), BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0)
        throw std::runtime_error("token_generation_failed");
#else
    for (size_t offset = 0; offset < sizeof(bytes);) {
        const auto count = getrandom(bytes + offset, sizeof(bytes) - offset, 0);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) throw std::runtime_error("token_generation_failed");
        offset += static_cast<size_t>(count);
    }
#endif
    constexpr char hex[] = "0123456789abcdef";
    std::string token;
    token.reserve(2 * sizeof(bytes));
    for (const auto byte : bytes) {
        token += hex[byte >> 4];
        token += hex[byte & 15];
    }
    return token;
}

// Call with stateMutex held. An active publication owns all its group's fields.
bool fieldIsPublished(const std::string& name) {
    for (const auto& entry : publications) {
        const auto& fields = entry.second.group->second;
        if (std::find(fields.begin(), fields.end(), name) != fields.end()) return true;
    }
    return false;
}

std::string publish(const std::string& name, Socket owner, const sockaddr_in& peer, int uploadPort) {
    const auto group = streamDefinitions.find(name);
    if (group == streamDefinitions.end()) throw std::runtime_error("unknown_publication");

    const std::string reply = "OK PUBLISH " + name + " " + std::to_string(uploadPort) + " ";
    std::lock_guard<std::mutex> lock(stateMutex);
    for (const auto& [token, publication] : publications) {
        if (publication.owner == owner && publication.group == group) return reply + token + "\n";
    }
    for (const auto& field : group->second) {
        if (!catalog.at(field).writable) throw std::runtime_error("write_denied");
        if (fieldIsPublished(field)) throw std::runtime_error("field_published");
    }
    // Insert once, after verifying every field, so a rejected request claims nothing.
    std::string token;
    do {
        token = publicationToken();
    } while (publications.count(token));
    const auto response = reply + token + "\n";
    publications.emplace(token, Publication{owner, peer.sin_addr.s_addr, group});
    return response;
}

void unpublish(Socket owner, const std::string& name) {
    std::lock_guard<std::mutex> lock(stateMutex);
    for (auto it = publications.begin(); it != publications.end(); ++it) {
        if (it->second.owner == owner && it->second.group->first == name) {
            publications.erase(it);
            return;
        }
    }
    throw std::runtime_error("publication_not_active");
}

std::string publicationStatus(Socket owner) {
    std::lock_guard<std::mutex> lock(stateMutex);
    const auto now = Publication::Clock::now();
    std::string reply = "PUBSTATUS";
    for (auto& entry : publications) {
        auto& publication = entry.second;
        if (publication.owner != owner) continue;
        publication.advanceWindow(now);
        reply += " " + publication.group->first + "=" +
                 (publication.previousAccepted ? std::to_string(*publication.previousAccepted) : "pending");
    }
    return reply + "\n";
}

std::pair<std::string, std::string> assignment(const std::string& text) {
    const auto equals = text.find('=');
    if (equals == std::string::npos) throw std::runtime_error("expected_name_equals_value");
    return {text.substr(0, equals), text.substr(equals + 1)};
}

void acceptSample(const std::string& packet, std::uint32_t sourceAddress) {
    const auto parts = tokens(packet);
    if (parts.size() < 4 || parts[0] != "SAMPLE" || parts[1].size() != 32) return;

    const auto& sequenceText = parts[2];
    const char* end = sequenceText.data() + sequenceText.size();
    std::uint64_t sequence = 0;
    const auto parsed = std::from_chars(sequenceText.data(), end, sequence);
    if (parsed.ec != std::errc{} || parsed.ptr != end || sequence == 0) return;

    std::lock_guard<std::mutex> lock(stateMutex);
    const auto found = publications.find(parts[1]);
    if (found == publications.end()) return;
    auto& publication = found->second;
    if (publication.peerAddress != sourceAddress || sequence <= publication.lastSequence) return;

    const auto& fields = publication.group->second;
    if (parts.size() - 3 != fields.size()) return;
    try {
        State candidate = state;
        std::set<std::string> seen;
        for (auto item = parts.begin() + 3; item != parts.end(); ++item) {
            const auto [name, text] = assignment(*item);
            if (std::find(fields.begin(), fields.end(), name) == fields.end() ||
                !seen.insert(name).second) return;
            candidate.at(name) = parseValue(name, catalog.at(name), text);
        }
        validate(candidate);
        state.swap(candidate);
        publication.lastSequence = sequence;
        publication.advanceWindow(Publication::Clock::now());
        ++publication.accepted;
    } catch (const std::runtime_error&) {
        // Invalid UDP samples are dropped without changing values or sequence.
    }
}

void receiveSamples(Socket udp) {
    for (;;) {
        sockaddr_in source{};
#ifdef _WIN32
        int sourceSize = sizeof(source);
#else
        socklen_t sourceSize = sizeof(source);
#endif
        // The extra byte lets us reject a truncated datagram on POSIX.
        char packet[maxSampleBytes + 1];
        const int count = static_cast<int>(recvfrom(udp, packet, sizeof(packet), 0,
            reinterpret_cast<sockaddr*>(&source), &sourceSize));
        if (count > 0 && count <= maxSampleBytes)
            acceptSample(std::string(packet, static_cast<size_t>(count)), source.sin_addr.s_addr);
    }
}

void subscribe(const std::vector<std::string>& args, Subscriptions& subscriptions) {
    if (args.size() != 3) throw std::runtime_error("expected_sub_port_and_name");
    const auto& portText = args[1];
    const char* end = portText.data() + portText.size();
    unsigned int port = 0;
    const auto parsed = std::from_chars(portText.data(), end, port);
    if (parsed.ec != std::errc{} || parsed.ptr != end || port == 0 || port > 65535)
        throw std::runtime_error("invalid_port");

    const auto& name = args[2];
    const auto group = streamDefinitions.find(name);
    if (group == streamDefinitions.end()) throw std::runtime_error("unknown_subscription");
    for (const auto& field : group->second) {
        if (!catalog.at(field).streamable) throw std::runtime_error("stream_denied");
    }
    // Repeating SUB for the same name changes its port and preserves its sequence.
    subscriptions[name].port = static_cast<unsigned short>(port);
}

// Dispatch one complete TCP command and return its newline-terminated reply.
std::string command(const std::string& line, Subscriptions& subscriptions, bool udpAvailable,
                    Socket owner, const sockaddr_in& peer, int uploadPort) {
    auto t=tokens(line);
    if(t.empty()) return "ERR empty_command\n";
    try {
        if(t[0]=="PING" && t.size()==1) return "OK PONG\n";
        if (t[0] == "SUB") {
            if (t.size() == 2 && t[1] == "false") {
                subscriptions.clear();
                return "OK SUB\n";
            }
            if (!udpAvailable) throw std::runtime_error("udp_unavailable");
            subscribe(t, subscriptions);
            return "OK SUB\n";
        }
        if (t[0] == "UNSUB" && t.size() == 2) {
            if (!subscriptions.erase(t[1])) return "ERR subscription_not_active\n";
            return "OK UNSUB\n";
        }
        if (t[0] == "PUBLISH" && t.size() == 2) return publish(t[1], owner, peer, uploadPort);
        if (t[0] == "UNPUBLISH" && t.size() == 2) {
            unpublish(owner, t[1]);
            return "OK UNPUBLISH\n";
        }
        if (t[0] == "PUBSTATUS" && t.size() == 1) return publicationStatus(owner);
        if(t[0]=="SCHEMA" && t.size()==1) {
            // The flag positions are fixed: stream (S), read (R), write (W).
            std::string out="SCHEMA";
            for(const auto& n:names) {
                const auto& field = catalog.at(n);
                // Keep the protocol v3 type label for the original weather field.
                const auto& type = n == "weather" && field.type == "enum" ? "weather" : field.type;
                out += " " + n + ":" + type + ":" + (field.streamable ? "S" : "-") +
                       (field.readable ? "R" : "-") + (field.writable ? "W" : "-");
            }
            return out+"\n";
        }
        // Keep GET snapshots consistent and serialize SET transactions.
        std::lock_guard<std::mutex> lock(stateMutex);
        if(t[0]=="GET" && t.size()>=2) {
            std::vector<std::string> fields;
            if (t.size() == 2 && t[1] == "*") {
                for (const auto& name : names) {
                    if (catalog.at(name).readable) fields.push_back(name);
                }
            } else {
                for (auto item = t.begin() + 1; item != t.end(); ++item) {
                    // Direct field names take precedence over groups with the same name.
                    const auto group = streamDefinitions.find(*item);
                    if (catalog.count(*item) || group == streamDefinitions.end()) {
                        fields.push_back(*item);
                    } else {
                        const auto& members = group->second;
                        fields.insert(fields.end(), members.begin(), members.end());
                    }
                }
            }
            std::string out="DATA";
            for(const auto& n:fields) {
                if(!catalog.count(n)) throw std::runtime_error("unknown_field");
                if(!catalog.at(n).readable) throw std::runtime_error("read_denied");
                out+=" "+n+"="+value(state,n);
            }
            return out+"\n";
        }
        if(t[0]=="SET" && t.size()>=2) {
            // Apply changes to a copy so any error leaves shared state intact.
            State candidate=state;
            for(size_t i=1;i<t.size();++i) {
                const auto [n, v] = assignment(t[i]);
                if(!catalog.count(n)) throw std::runtime_error("unknown_field");
                if(!catalog.at(n).writable) throw std::runtime_error("write_denied");
                if (fieldIsPublished(n)) throw std::runtime_error("field_published");
                candidate.at(n) = parseValue(n, catalog.at(n), v);
            }
            validate(candidate); state.swap(candidate); return "OK SET\n";
        }
        return "ERR invalid_command\n";
    } catch(const std::exception& e) { return std::string("ERR ")+e.what()+"\n"; }
}
// A TCP send can write only part of the message. Send the rest before returning.
bool sendLine(Socket s, const std::string& data) {
    size_t offset=0;
    while(offset<data.size()) {
#ifdef _WIN32
        int flags=0;
#else
        int flags=MSG_NOSIGNAL;
#endif
        int sent=static_cast<int>(send(s,data.data()+offset,static_cast<int>(data.size()-offset),flags));
        if(sent<=0) return false;
        offset+=static_cast<size_t>(sent);
    }
    return true;
}
std::atomic<int> activeClients{0};

Socket openStreamSocket() {
    Socket udp = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (udp == badSocket) return badSocket;
    // Drop a tick if the local send buffer is full. Keep TCP commands responsive.
#ifdef _WIN32
    u_long nonblocking = 1;
    if (ioctlsocket(udp, FIONBIO, &nonblocking) == 0) return udp;
#else
    if (fcntl(udp, F_SETFL, O_NONBLOCK) == 0) return udp;
#endif
    closeSocket(udp);
    return badSocket;
}

// One worker handles the client's TCP commands and sends its UDP updates.
void serve(Socket s, sockaddr_in peer, int uploadPort) {
    Socket udp = openStreamSocket();
    struct Cleanup {
        Socket tcp, udp;
        ~Cleanup() {
            {
                std::lock_guard<std::mutex> lock(stateMutex);
                for (auto it = publications.begin(); it != publications.end();) {
                    if (it->second.owner == tcp) it = publications.erase(it);
                    else ++it;
                }
            }
            if (udp != badSocket) closeSocket(udp);
            closeSocket(tcp);
            --activeClients;
        }
    } cleanup{s, udp};
    // Bound how long a slow client can block this worker's send.
#ifdef _WIN32
    DWORD timeout=1000; setsockopt(s,SOL_SOCKET,SO_SNDTIMEO,reinterpret_cast<const char*>(&timeout),sizeof(timeout));
#else
    timeval timeout{1,0}; setsockopt(s,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
#endif
    if(!sendLine(s,"HELLO 3\n")) return;
    // Schedule 60 updates per second using a clock unaffected by wall-clock changes.
    using Clock=std::chrono::steady_clock;
    const auto period=std::chrono::nanoseconds(16666667);
    auto next=Clock::now()+period;
    Subscriptions subscriptions;
    std::string pending;
    for(;;) {
        auto now=Clock::now();
        if(now>=next) {
            if (!subscriptions.empty()) {
                State snapshot;
                {
                    // Copy only subscribed fields, all from the same point in time.
                    std::lock_guard<std::mutex> lock(stateMutex);
                    for (const auto& entry : subscriptions) {
                        for (const auto& field : streamDefinitions.at(entry.first))
                            snapshot.try_emplace(field, state.at(field));
                    }
                }
                for (auto& [name, subscription] : subscriptions) {
                    std::string out = "UPDATE " + name + " " + std::to_string(++subscription.sequence);
                    for (const auto& field : streamDefinitions.at(name))
                        out += " " + field + "=" + value(snapshot, field);
                    out += '\n';
                    // Use the TCP peer's IP. SUB controls only the destination port.
                    peer.sin_port = htons(subscription.port);
                    sendto(udp, out.data(), static_cast<int>(out.size()), 0,
                           reinterpret_cast<const sockaddr*>(&peer), sizeof(peer));
                }
            }
            next+=period;
            // Skip missed ticks to avoid sending a burst after a delay.
            if(next<=Clock::now()) next=Clock::now()+period;
        }
        // Wait for a command or the next stream deadline, whichever comes first.
        auto wait=std::chrono::duration_cast<std::chrono::microseconds>(next-Clock::now()).count();
        wait=std::max<long long>(0,wait);
        timeval tv{static_cast<long>(wait/1000000),static_cast<long>(wait%1000000)};
        fd_set readSet; FD_ZERO(&readSet); FD_SET(s,&readSet);
        // With no subscription, sleep until the TCP socket has work.
        int ready=select(static_cast<int>(s+1),&readSet,nullptr,nullptr,
                         subscriptions.empty() ? nullptr : &tv);
        if(ready<0) return;
        if(ready==0) continue;
        char buffer[4096]; int count=static_cast<int>(recv(s,buffer,sizeof(buffer),0));
        if(count<=0) return;
        // A TCP read can contain part of a line or several lines. Keep any
        // unfinished command in pending until its newline arrives.
        pending.append(buffer,static_cast<size_t>(count));
        size_t end;
        while((end=pending.find('\n'))!=std::string::npos) {
            if(end>4096) { sendLine(s,"ERR line_too_long\n"); return; }
            auto line=pending.substr(0,end); pending.erase(0,end+1);
            if(!sendLine(s,command(line,subscriptions,udp != badSocket,s,peer,uploadPort))) return;
        }
        if(pending.size()>4096) { sendLine(s,"ERR line_too_long\n"); return; }
    }
}
template <typename Read>
void loadConfig(const std::filesystem::path& path, Read read) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("cannot open " + path.string());
    try {
        ConfigReader reader(input);
        read(reader);
    } catch (const std::runtime_error& error) {
        throw std::runtime_error(path.string() + ": " + error.what());
    }
}

int main(int argc, char** argv) {
    try {
        if(argc>5) throw std::runtime_error("usage: data_server [port] [fields_json] [bind_ipv4] [field_groups_json]");
        int port=9000;
        if(argc>1) { size_t used; std::string arg=argv[1]; port=std::stoi(arg,&used); if(used!=arg.size()) throw std::runtime_error("invalid_port"); }
        if(port<1 || port>65535) throw std::runtime_error("invalid_port");
        std::filesystem::path configDirectory;
#ifdef DATA_SERVER_LAN
        constexpr const char* defaultAddress = "100.100.110.2";
        wchar_t executable[32768];
        const auto length = GetModuleFileNameW(nullptr, executable, 32768);
        if (length == 0 || length >= 32768) throw std::runtime_error("cannot locate the LAN executable");
        configDirectory = std::filesystem::path(executable).parent_path();
#else
        constexpr const char* defaultAddress = "127.0.0.1";
#endif
        const char* bindAddress = argc > 3 ? argv[3] : defaultAddress;
        const auto endpoint = std::string(bindAddress) + ':' + std::to_string(port);
        const auto catalogPath = argc > 2 ? std::filesystem::path(argv[2]) : configDirectory / "fields.json";
        const auto groupsPath = argc > 4 ? std::filesystem::path(argv[4]) : configDirectory / "field_groups.json";
        loadConfig(catalogPath, [](ConfigReader& reader) { reader.readCatalog(); });
        loadConfig(groupsPath, [](ConfigReader& reader) { streamDefinitions = reader.readGroups(); });
#ifdef _WIN32
        WSADATA wsa; if(WSAStartup(MAKEWORD(2,2),&wsa)!=0) throw std::runtime_error("WSAStartup_failed");
#endif
        // Open an IPv4 TCP listener using the selected build's default or CLI override.
        Socket listener=socket(AF_INET,SOCK_STREAM,IPPROTO_TCP);
        if(listener==badSocket) throw std::runtime_error("socket_failed");
        sockaddr_in address{}; address.sin_family=AF_INET; address.sin_port=htons(static_cast<unsigned short>(port));
        if(inet_pton(AF_INET,bindAddress,&address.sin_addr)!=1) throw std::runtime_error("invalid_bind_address");
        if(bind(listener,reinterpret_cast<sockaddr*>(&address),sizeof(address))!=0 || listen(listener,32)!=0) {
            closeSocket(listener); throw std::runtime_error("cannot listen on TCP " + endpoint);
        }
        // TCP and UDP can share the same address and port.
        Socket uploadSocket = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (uploadSocket == badSocket ||
            bind(uploadSocket, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0) {
            if (uploadSocket != badSocket) closeSocket(uploadSocket);
            closeSocket(listener);
            throw std::runtime_error("cannot listen on UDP " + endpoint);
        }
        try {
            std::thread(receiveSamples, uploadSocket).detach();
        } catch (...) {
            closeSocket(uploadSocket);
            closeSocket(listener);
            throw;
        }
        std::cout << "Listening on " << endpoint << std::endl;
        for(;;) {
            sockaddr_in peer{};
#ifdef _WIN32
            int peerSize = sizeof(peer);
#else
            socklen_t peerSize = sizeof(peer);
#endif
            Socket client=accept(listener,reinterpret_cast<sockaddr*>(&peer),&peerSize);
            if(client==badSocket) continue;
#ifndef _WIN32
            if(client>=FD_SETSIZE) { closeSocket(client); continue; }
#endif
            // Reserve a client slot before handing off ownership of the socket.
            if(activeClients.fetch_add(1)>=64) { --activeClients; closeSocket(client); continue; }
            try { std::thread(serve,client,peer,port).detach(); }
            catch(...) { --activeClients; closeSocket(client); }
        }
    } catch(const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
