package main

import (
	"log/slog"

	"weave-os/router/internal/config"
	"weave-os/router/internal/router"
	"weave-os/router/internal/router/jev"
)

const defaultJEVBaseURL = "https://openrouter.ai/api/v1"

// buildJEVRouter registers the JEV strategy only when a judge credential is configured.
func buildJEVRouter(logger *slog.Logger) router.Router {
	apiKey := config.GetOr("ROUTER_JEV_API_KEY", config.GetOr("OPENROUTER_API_KEY", ""))
	if apiKey == "" {
		logger.Info("JEV routing model disabled: no judge credential")
		return nil
	}
	baseURL := config.GetOr("ROUTER_JEV_BASE_URL", config.GetOr("OPENROUTER_BASE_URL", defaultJEVBaseURL))
	timeout := parseEnvDurationMs("ROUTER_JEV_TIMEOUT_MS", jev.DefaultTimeout)
	logger.Info("JEV routing model enabled", "base_url", baseURL, "timeout", timeout)
	return jev.New(jev.NewClient(baseURL, apiKey, timeout))
}
