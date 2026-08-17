package api

import (
	"crypto/subtle"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"whatsapp-bridge/internal/security"
)

// Rate limiter state
var (
	rateLimitMu     sync.Mutex
	requestCounts   = make(map[string]int)
	requestWindows  = make(map[string]time.Time)
	rateLimit       = 100 // requests per window
	rateLimitWindow = time.Minute
)

// clientIP extracts the caller's IP address, stripping the ephemeral source
// port that net/http bakes into RemoteAddr (e.g. "127.0.0.1:54321").
//
// Keying the rate-limit maps on the raw RemoteAddr was an unbounded-cache
// leak: every request from a client that opens a fresh TCP connection (which
// includes every call the Python MCP server makes via bare `requests.get`/
// `requests.post`, since those don't pool connections the way a `Session`
// does) gets a new ephemeral port and therefore a brand-new map entry that
// was never evicted. On a bridge fielding tens of thousands of MCP tool
// calls a day, that grows the process memory without bound. Stripping the
// port collapses those back down to the small number of real client IPs
// (almost always just 127.0.0.1) and restores the rate limiter's actual
// intent — it was never really limiting anything per-connection.
func clientIP(r *http.Request) string {
	ip := r.RemoteAddr
	if forwarded := r.Header.Get("X-Forwarded-For"); forwarded != "" {
		ip = strings.TrimSpace(strings.Split(forwarded, ",")[0])
	}
	if host, _, err := net.SplitHostPort(ip); err == nil {
		return host
	}
	return ip
}

// startRateLimitJanitor prunes stale rate-limit map entries on a timer.
// Defense in depth alongside clientIP: even a small, bounded set of real
// client IPs would otherwise sit in these maps forever once a window has
// passed with no further requests from that IP.
func startRateLimitJanitor() {
	go func() {
		ticker := time.NewTicker(rateLimitWindow)
		defer ticker.Stop()
		for range ticker.C {
			cutoff := time.Now().Add(-rateLimitWindow)
			rateLimitMu.Lock()
			for ip, window := range requestWindows {
				if window.Before(cutoff) {
					delete(requestWindows, ip)
					delete(requestCounts, ip)
				}
			}
			rateLimitMu.Unlock()
		}
	}()
}

func init() {
	startRateLimitJanitor()
}

// getAllowedOrigins returns the list of allowed CORS origins
func getAllowedOrigins() map[string]bool {
	origins := map[string]bool{
		"http://localhost:8089":   true, // Webhook UI (localhost)
		"http://localhost:8090":   true, // Pairing UI (localhost)
		"http://127.0.0.1:8089":  true, // Webhook UI (IP — browsers use IP when accessed via 127.0.0.1)
		"http://127.0.0.1:8090":  true, // Pairing UI (IP)
	}

	// Allow additional origins from env var (comma-separated)
	if extra := os.Getenv("CORS_ORIGINS"); extra != "" {
		for _, origin := range strings.Split(extra, ",") {
			origins[strings.TrimSpace(origin)] = true
		}
	}

	return origins
}

// AuthMiddleware validates API key authentication using constant-time comparison
func AuthMiddleware(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		expectedKey := os.Getenv("API_KEY")

		// Skip auth if no API_KEY is configured (dev mode)
		if expectedKey == "" {
			next(w, r)
			return
		}

		// Get client IP
		ip := clientIP(r)

		// Check X-API-Key header using constant-time comparison to prevent timing attacks
		apiKey := r.Header.Get("X-API-Key")
		if subtle.ConstantTimeCompare([]byte(apiKey), []byte(expectedKey)) != 1 {
			security.LogAuthFailure(ip, r.Header.Get("User-Agent"), "Invalid API key")
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}

		security.LogAuthSuccess(ip, r.URL.Path)
		next(w, r)
	}
}

// RateLimitMiddleware limits requests per IP address
func RateLimitMiddleware(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// Get client IP
		ip := clientIP(r)

		rateLimitMu.Lock()
		now := time.Now()

		// Reset window if expired
		if window, exists := requestWindows[ip]; !exists || now.Sub(window) > rateLimitWindow {
			requestWindows[ip] = now
			requestCounts[ip] = 0
		}

		requestCounts[ip]++
		count := requestCounts[ip]
		rateLimitMu.Unlock()

		if count > rateLimit {
			security.LogRateLimitExceeded(ip)
			w.Header().Set("Retry-After", "60")
			http.Error(w, "Rate limit exceeded", http.StatusTooManyRequests)
			return
		}

		next(w, r)
	}
}

// CorsMiddleware adds CORS headers with restricted origins
func CorsMiddleware(next http.HandlerFunc) http.HandlerFunc {
	allowedOrigins := getAllowedOrigins()

	return func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")

		// Check if origin is allowed
		if allowedOrigins[origin] {
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Set("Access-Control-Allow-Credentials", "true")
		}
		// If origin not allowed, don't set Access-Control-Allow-Origin (browser blocks)

		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")
		w.Header().Set("Access-Control-Max-Age", "86400")

		// Handle preflight requests
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusOK)
			return
		}

		next(w, r)
	}
}

// SecurityHeadersMiddleware adds security headers to all responses
func SecurityHeadersMiddleware(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// Prevent MIME type sniffing
		w.Header().Set("X-Content-Type-Options", "nosniff")
		// Prevent clickjacking
		w.Header().Set("X-Frame-Options", "DENY")
		// XSS protection (legacy but still useful)
		w.Header().Set("X-XSS-Protection", "1; mode=block")
		// Referrer policy
		w.Header().Set("Referrer-Policy", "strict-origin-when-cross-origin")
		// Content Security Policy for API
		w.Header().Set("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
		// Permissions policy
		w.Header().Set("Permissions-Policy", "geolocation=(), microphone=(), camera=()")

		next(w, r)
	}
}

// SecureMiddleware chains security headers, auth, rate limiting, and CORS middleware
func SecureMiddleware(next http.HandlerFunc) http.HandlerFunc {
	return SecurityHeadersMiddleware(CorsMiddleware(RateLimitMiddleware(AuthMiddleware(next))))
}
