FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags='-s -w' -o /out/runnergo-agent ./cmd/agent-server

FROM alpine:3.20
RUN addgroup -S runnergo && adduser -S -G runnergo runnergo && apk add --no-cache ca-certificates wget
COPY --from=build /out/runnergo-agent /usr/local/bin/runnergo-agent
USER runnergo
EXPOSE 8088
HEALTHCHECK --interval=10s --timeout=3s --retries=12 CMD wget -qO- http://127.0.0.1:8088/health || exit 1
ENTRYPOINT ["/usr/local/bin/runnergo-agent"]
