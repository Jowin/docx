# The console: built once, served by `vite preview`, which proxies /api/runtime and /api/designtime.
FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build
ENV RUNTIME_URL=http://runtime:8000 DESIGNTIME_URL=http://api:8000
EXPOSE 4173
CMD ["npx", "vite", "preview", "--host", "0.0.0.0", "--port", "4173"]
