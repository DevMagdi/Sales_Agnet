/** Dashboard behind the same origin as the API; in dev we proxy to the backend. */
const backend = process.env.BACKEND_URL || "http://127.0.0.1:8000";

const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [
      { source: "/api/v1/:path*", destination: `${backend}/api/v1/:path*` },
      { source: "/healthz", destination: `${backend}/healthz` },
      { source: "/metrics", destination: `${backend}/metrics` },
    ];
  },
};

export default nextConfig;
