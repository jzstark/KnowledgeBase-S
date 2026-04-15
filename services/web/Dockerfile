FROM node:20-alpine AS base

WORKDIR /app
COPY package.json package-lock.json* ./

# 开发阶段（docker-compose.dev.yml 指定 target: dev）
FROM base AS dev
RUN npm install && mv /app/node_modules /node_modules
ENV NODE_PATH=/node_modules
ENV PATH=/node_modules/.bin:$PATH
COPY . .
CMD ["next", "dev"]

# 构建阶段
FROM base AS builder
RUN npm ci
COPY . .
RUN mkdir -p public && npm run build

# 生产镜像
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production

RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
ENV PORT=3000

CMD ["node", "server.js"]
