#pragma once

#include <cctype>
#include <cmath>
#include <cstdint>
#include <map>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace eyes {

class Json {
public:
    enum class Type { Null, Boolean, Number, String, Array, Object };

    Json() = default;

    static Json parse(const std::string& text) {
        Parser parser(text);
        Json value = parser.value();
        parser.space();
        if (!parser.done()) throw std::runtime_error("JSON contains trailing data");
        return value;
    }

    Type type() const { return type_; }
    bool is_null() const { return type_ == Type::Null; }
    bool is_array() const { return type_ == Type::Array; }
    bool is_object() const { return type_ == Type::Object; }

    bool boolean(bool fallback = false) const {
        return type_ == Type::Boolean ? boolean_ : fallback;
    }
    double number(double fallback = 0) const {
        return type_ == Type::Number ? number_ : fallback;
    }
    std::int64_t integer(std::int64_t fallback = 0) const {
        return type_ == Type::Number ? static_cast<std::int64_t>(number_) : fallback;
    }
    const std::string& string() const {
        static const std::string empty;
        return type_ == Type::String ? string_ : empty;
    }
    const std::vector<Json>& array() const {
        static const std::vector<Json> empty;
        return type_ == Type::Array ? array_ : empty;
    }
    const std::map<std::string, Json>& object() const {
        static const std::map<std::string, Json> empty;
        return type_ == Type::Object ? object_ : empty;
    }
    const Json& operator[](std::string_view key) const {
        static const Json null;
        if (type_ != Type::Object) return null;
        auto it = object_.find(std::string(key));
        return it == object_.end() ? null : it->second;
    }

private:
    class Parser {
    public:
        explicit Parser(const std::string& input) : input_(input) {}

        bool done() const { return pos_ >= input_.size(); }
        void space() {
            while (!done() && std::isspace(static_cast<unsigned char>(input_[pos_]))) ++pos_;
        }

        Json value() {
            space();
            if (done()) fail("unexpected end");
            switch (input_[pos_]) {
            case 'n': return literal("null", Json{});
            case 't': return literal("true", make_boolean(true));
            case 'f': return literal("false", make_boolean(false));
            case '"': return make_string(parse_string());
            case '[': return parse_array();
            case '{': return parse_object();
            default:
                if (input_[pos_] == '-' || std::isdigit(static_cast<unsigned char>(input_[pos_]))) {
                    return parse_number();
                }
                fail("unexpected token");
            }
        }

    private:
        [[noreturn]] void fail(const char* message) const {
            throw std::runtime_error(std::string(message) + " at byte " + std::to_string(pos_));
        }
        bool consume(char expected) {
            space();
            if (!done() && input_[pos_] == expected) {
                ++pos_;
                return true;
            }
            return false;
        }
        Json literal(std::string_view token, Json result) {
            if (input_.compare(pos_, token.size(), token) != 0) fail("invalid literal");
            pos_ += token.size();
            return result;
        }
        Json parse_number() {
            const std::size_t start = pos_;
            if (input_[pos_] == '-') ++pos_;
            while (!done() && std::isdigit(static_cast<unsigned char>(input_[pos_]))) ++pos_;
            if (!done() && input_[pos_] == '.') {
                ++pos_;
                while (!done() && std::isdigit(static_cast<unsigned char>(input_[pos_]))) ++pos_;
            }
            if (!done() && (input_[pos_] == 'e' || input_[pos_] == 'E')) {
                ++pos_;
                if (!done() && (input_[pos_] == '+' || input_[pos_] == '-')) ++pos_;
                while (!done() && std::isdigit(static_cast<unsigned char>(input_[pos_]))) ++pos_;
            }
            Json result;
            result.type_ = Type::Number;
            result.number_ = std::stod(input_.substr(start, pos_ - start));
            return result;
        }
        static void append_utf8(std::string& output, unsigned codepoint) {
            if (codepoint <= 0x7f) output.push_back(static_cast<char>(codepoint));
            else if (codepoint <= 0x7ff) {
                output.push_back(static_cast<char>(0xc0 | (codepoint >> 6)));
                output.push_back(static_cast<char>(0x80 | (codepoint & 0x3f)));
            } else {
                output.push_back(static_cast<char>(0xe0 | (codepoint >> 12)));
                output.push_back(static_cast<char>(0x80 | ((codepoint >> 6) & 0x3f)));
                output.push_back(static_cast<char>(0x80 | (codepoint & 0x3f)));
            }
        }
        std::string parse_string() {
            if (!consume('"')) fail("expected string");
            std::string result;
            while (!done()) {
                char ch = input_[pos_++];
                if (ch == '"') return result;
                if (ch != '\\') {
                    result.push_back(ch);
                    continue;
                }
                if (done()) fail("invalid escape");
                ch = input_[pos_++];
                switch (ch) {
                case '"': result.push_back('"'); break;
                case '\\': result.push_back('\\'); break;
                case '/': result.push_back('/'); break;
                case 'b': result.push_back('\b'); break;
                case 'f': result.push_back('\f'); break;
                case 'n': result.push_back('\n'); break;
                case 'r': result.push_back('\r'); break;
                case 't': result.push_back('\t'); break;
                case 'u': {
                    if (pos_ + 4 > input_.size()) fail("invalid unicode escape");
                    unsigned codepoint = 0;
                    for (int i = 0; i < 4; ++i) {
                        const char hex = input_[pos_++];
                        codepoint <<= 4;
                        if (hex >= '0' && hex <= '9') codepoint |= static_cast<unsigned>(hex - '0');
                        else if (hex >= 'a' && hex <= 'f') codepoint |= static_cast<unsigned>(hex - 'a' + 10);
                        else if (hex >= 'A' && hex <= 'F') codepoint |= static_cast<unsigned>(hex - 'A' + 10);
                        else fail("invalid unicode escape");
                    }
                    append_utf8(result, codepoint);
                    break;
                }
                default: fail("invalid escape");
                }
            }
            fail("unterminated string");
        }
        Json parse_array() {
            consume('[');
            Json result;
            result.type_ = Type::Array;
            if (consume(']')) return result;
            for (;;) {
                result.array_.push_back(value());
                if (consume(']')) return result;
                if (!consume(',')) fail("expected comma");
            }
        }
        Json parse_object() {
            consume('{');
            Json result;
            result.type_ = Type::Object;
            if (consume('}')) return result;
            for (;;) {
                space();
                if (done() || input_[pos_] != '"') fail("expected object key");
                std::string key = parse_string();
                if (!consume(':')) fail("expected colon");
                result.object_.emplace(std::move(key), value());
                if (consume('}')) return result;
                if (!consume(',')) fail("expected comma");
            }
        }
        static Json make_boolean(bool value) {
            Json result;
            result.type_ = Type::Boolean;
            result.boolean_ = value;
            return result;
        }
        static Json make_string(std::string value) {
            Json result;
            result.type_ = Type::String;
            result.string_ = std::move(value);
            return result;
        }

        const std::string& input_;
        std::size_t pos_ = 0;
    };

    Type type_ = Type::Null;
    bool boolean_ = false;
    double number_ = 0;
    std::string string_;
    std::vector<Json> array_;
    std::map<std::string, Json> object_;
};

} // namespace eyes
