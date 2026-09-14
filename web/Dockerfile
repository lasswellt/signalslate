FROM node:20-slim AS build
WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . .
RUN npm run build

FROM node:20-slim
WORKDIR /app
COPY --from=build /app/.output ./.output
ENV NUXT_PUBLIC_API_BASE=http://api:8000
EXPOSE 3000
CMD ["node", ".output/server/index.mjs"]
