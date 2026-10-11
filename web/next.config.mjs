// The Next.js app is the UI only. Every /api/* request is proxied to the
// FastAPI backend (agentfinx serve), which owns accounts, sessions, chat runs,
// imports and report discovery. The browser stays same-origin, so the
// HttpOnly session cookie set by FastAPI works unchanged. FastAPI checks the
// Origin header: add this app's origin (default http://127.0.0.1:3000) to
// FINX_ALLOWED_ORIGINS.
//
// FINX_API_URL is read at BUILD time (`npm run build`, or `npm run dev`): the
// rewrite below is compiled into the build, and `env` inlines the same value
// for server components, so both always talk to the same backend. Rebuild to
// point at another backend.
//
// The proxy passes a client-sent X-Forwarded-For through unchanged and adds
// none, so FastAPI must not trust it unless a reverse proxy in front of this
// app overwrites it (see FINX_TRUSTED_PROXIES in webapp/README.md).
const api = (process.env.FINX_API_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  env: { FINX_API_URL: api },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${api}/api/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "same-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
