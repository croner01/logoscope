FROM golang:1.22-alpine AS builder

WORKDIR /src/ingest-service

COPY ingest-service/go.mod ingest-service/go.sum ./
RUN go mod download

COPY ingest-service/cmd ./cmd
COPY ingest-service/internal ./internal
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/ingest-service ./cmd/ingest

FROM alpine:3.20

WORKDIR /app

ENV TZ=UTC

COPY --from=builder /out/ingest-service /usr/local/bin/ingest-service

RUN addgroup -S ingest && adduser -S -G ingest ingest \
    && chown ingest:ingest /usr/local/bin/ingest-service

USER ingest

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/health || exit 1

CMD ["/usr/local/bin/ingest-service"]
