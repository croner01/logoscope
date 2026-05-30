package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"

	ingest "logoscope/ingest-service/internal/ingest"
)

func main() {
	cfg := ingest.LoadConfig()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if err := ingest.Run(ctx, cfg); err != nil {
		log.Printf("[ingest-go] fatal: %v", err)
		os.Exit(1)
	}
}
