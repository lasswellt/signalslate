FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
# npm ci, not install: without the lockfile present, npm 10's fresh dependency
# resolution can crash outright ("Cannot read properties of null (reading
# 'edgesOut')", a known npm bug on some trees) instead of just being slow.
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM node:20-slim
WORKDIR /app
COPY --from=build /app/.output ./.output
ENV NUXT_PUBLIC_API_BASE=http://api:8000
EXPOSE 3000
CMD ["node", ".output/server/index.mjs"]
