// A test-only stand-in for Spine's intercept endpoint.
//
// Real HTTP/1.1 over a loopback socket, so the client's actual libcurl path
// is exercised, including keep-alive. Counts accepted connections and
// requests, which is how the tests prove connection reuse and that a tripped
// breaker makes no network call. POSIX only; never linked into the library.

#ifndef SPINE_TESTS_FAKE_SPINE_HPP
#define SPINE_TESTS_FAKE_SPINE_HPP

#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cstdlib>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace spine_test {

inline const char* kAllowed =
    R"({"allowed":true,"decision":"allowed","reason":"ok","audit_event_id":"ev-1"})";
inline const char* kBlocked =
    R"({"allowed":false,"decision":"blocked","reason":"Restricted list"})";
inline const char* kFlagged =
    R"({"allowed":false,"decision":"flagged","reason":"Four-eyes review"})";

class FakeSpine {
public:
    FakeSpine() {
        listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
        int one = 1;
        ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        addr.sin_port = 0;  // any free port
        ::bind(listen_fd_, reinterpret_cast<sockaddr*>(&addr), sizeof addr);
        ::listen(listen_fd_, 64);
        socklen_t len = sizeof addr;
        ::getsockname(listen_fd_, reinterpret_cast<sockaddr*>(&addr), &len);
        port_ = ntohs(addr.sin_port);
        acceptor_ = std::thread([this] { accept_loop(); });
    }

    ~FakeSpine() { stop(); }
    FakeSpine(const FakeSpine&) = delete;
    FakeSpine& operator=(const FakeSpine&) = delete;

    std::string url() const { return "http://127.0.0.1:" + std::to_string(port_); }

    void respond(int status, std::string body) {
        std::lock_guard<std::mutex> g(mu_);
        status_ = status;
        body_ = std::move(body);
    }
    void set_delay(std::chrono::milliseconds d) { delay_ms_ = static_cast<int>(d.count()); }
    void set_close_each(bool v) { close_each_ = v; }

    int connections() const { return connections_.load(); }
    int requests() const { return requests_.load(); }
    std::string last_body() const {
        std::lock_guard<std::mutex> g(mu_);
        return last_body_;
    }

    void stop() {
        if (stopped_.exchange(true)) return;
        if (acceptor_.joinable()) acceptor_.join();
        ::close(listen_fd_);
        std::vector<std::thread> workers;
        {
            std::lock_guard<std::mutex> g(mu_);
            workers.swap(workers_);
        }
        for (auto& t : workers) t.join();
    }

private:
    // Poll with a short timeout rather than block, so stop() works without
    // closing sockets out from under another thread (portable to macOS).
    bool readable(int fd) {
        while (!stopped_) {
            pollfd p{fd, POLLIN, 0};
            const int n = ::poll(&p, 1, 20);
            if (n > 0) return true;
            if (n < 0) return false;
        }
        return false;
    }

    void accept_loop() {
        while (readable(listen_fd_)) {
            const int fd = ::accept(listen_fd_, nullptr, nullptr);
            if (fd < 0) continue;
            ++connections_;
            std::lock_guard<std::mutex> g(mu_);
            workers_.emplace_back([this, fd] { serve(fd); });
        }
    }

    bool read_more(int fd, std::string* buf) {
        if (!readable(fd)) return false;
        char chunk[4096];
        const ssize_t n = ::recv(fd, chunk, sizeof chunk, 0);
        if (n <= 0) return false;
        buf->append(chunk, static_cast<size_t>(n));
        return true;
    }

    static size_t content_length(const std::string& headers) {
        std::string lower(headers);
        std::transform(lower.begin(), lower.end(), lower.begin(),
                       [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        const size_t pos = lower.find("content-length:");
        if (pos == std::string::npos) return 0;
        return static_cast<size_t>(std::strtoul(lower.c_str() + pos + 15, nullptr, 10));
    }

    void send_all(int fd, const std::string& data) {
#ifdef MSG_NOSIGNAL
        const int flags = MSG_NOSIGNAL;
#else
        const int flags = 0;
        int one = 1;
        ::setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#endif
        size_t sent = 0;
        while (sent < data.size()) {
            const ssize_t n = ::send(fd, data.data() + sent, data.size() - sent, flags);
            if (n <= 0) return;
            sent += static_cast<size_t>(n);
        }
    }

    void serve(int fd) {
        std::string buf;
        for (;;) {
            size_t header_end;
            while ((header_end = buf.find("\r\n\r\n")) == std::string::npos) {
                if (!read_more(fd, &buf)) {
                    ::close(fd);
                    return;
                }
            }
            const size_t len = content_length(buf.substr(0, header_end));
            const size_t total = header_end + 4 + len;
            while (buf.size() < total) {
                if (!read_more(fd, &buf)) {
                    ::close(fd);
                    return;
                }
            }
            int status;
            std::string body;
            {
                std::lock_guard<std::mutex> g(mu_);
                last_body_ = buf.substr(header_end + 4, len);
                status = status_;
                body = body_;
            }
            buf.erase(0, total);
            ++requests_;

            const int delay = delay_ms_.load();
            if (delay > 0) std::this_thread::sleep_for(std::chrono::milliseconds(delay));

            const bool close_after = close_each_.load();
            std::string resp = "HTTP/1.1 " + std::to_string(status) + " X\r\n"
                               "Content-Type: application/json\r\n"
                               "Content-Length: " + std::to_string(body.size()) + "\r\n";
            if (close_after) resp += "Connection: close\r\n";
            resp += "\r\n" + body;
            send_all(fd, resp);
            if (close_after) {
                ::close(fd);
                return;
            }
        }
    }

    int listen_fd_ = -1;
    int port_ = 0;
    std::thread acceptor_;
    std::vector<std::thread> workers_;
    mutable std::mutex mu_;
    int status_ = 200;
    std::string body_ = kAllowed;
    std::string last_body_;
    std::atomic<int> delay_ms_{0};
    std::atomic<bool> close_each_{false};
    std::atomic<bool> stopped_{false};
    std::atomic<int> connections_{0};
    std::atomic<int> requests_{0};
};

}  // namespace spine_test

#endif  // SPINE_TESTS_FAKE_SPINE_HPP
