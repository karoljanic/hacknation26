FROM node:22-alpine

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=secret,id=npm_ca \
    if [ -f /run/secrets/npm_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/npm_ca; fi; \
    npm ci
COPY . .
RUN npm run build

USER node
CMD ["npm", "run", "start"]
