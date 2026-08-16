/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async redirects() {
    return [{ source: "/", destination: "/chat", permanent: false }];
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.BACKEND_URL || "http://127.0.0.1:8000"}/api/:path*`,
      },
      {
        source: "/files/:path*",
        destination: `${process.env.BACKEND_URL || "http://127.0.0.1:8000"}/files/:path*`,
      },
    ];
  },
};
export default nextConfig;
