# The dashboard: built with Node, served by nginx.
#
# The first stage compiles the React app to static files. The second holds only
# those files and an nginx that runs as a non-root user on an unprivileged port.
FROM node:24-alpine AS build
WORKDIR /app
# Dependencies first: this layer is rebuilt only when the lock file changes.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build


FROM nginxinc/nginx-unprivileged:1.29-alpine
# Readable by nginx's unprivileged user whatever the file's permissions were
# in the checkout it was built from.
COPY --chmod=0644 nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
