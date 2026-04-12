/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  // API 请求在容器内走内网，直接转发
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.API_URL || "http://api:8000"}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
