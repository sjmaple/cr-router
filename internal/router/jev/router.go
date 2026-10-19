package jev

import (
	"context"
	"fmt"
	"strings"
	"sync"

	"weave-os/router/internal/providers"
	"weave-os/router/internal/router"
	"weave-os/router/internal/router/catalog"
)

// EscalationCadence checks for escalation on every third logical call.
const EscalationCadence = 3

const maxTrackedSessions = 10_000

// Harness selects the model lineup for the calling coding agent.
type Harness string

const (
	HarnessClaudeCode Harness = "claude_code"
	HarnessCodex      Harness = "codex"
)

const clientAppCodex = "codex"

// Lineup is one harness's models: Top for high-task judgment and escalation,
// Fast for defined work, LowTaskJudgment for judgment calls on low tasks.
type Lineup struct {
	Top             string
	Fast            string
	LowTaskJudgment string
	Subscription    string
}

// Lineups pins each harness's models.
var Lineups = map[Harness]Lineup{
	HarnessClaudeCode: {Top: string(catalog.ModelIDClaudeOpus48), Fast: string(catalog.ModelIDClaudeHaiku45), LowTaskJudgment: "z-ai/glm-5.3", Subscription: providers.ProviderAnthropic},
	HarnessCodex:      {Top: "gpt-6.1-sol", Fast: string(catalog.ModelIDGPT56Luna), LowTaskJudgment: "z-ai/glm-5.3", Subscription: providers.ProviderOpenAI},
}

type sessionState struct {
	task      Choice
	escalated bool
	fastSince bool
}

// Router asks JEV for task complexity, call complexity and periodic escalation,
// then picks the model in Go.
type Router struct {
	classifier Classifier
	mu         sync.Mutex
	sessions   map[string]sessionState
	order      []string
}

// New returns a JEV router backed by classifier.
func New(classifier Classifier) *Router {
	return &Router{classifier: classifier, sessions: map[string]sessionState{}}
}

// HarnessFor maps a client app to its lineup.
func HarnessFor(clientApp string) Harness {
	if strings.EqualFold(clientApp, clientAppCodex) {
		return HarnessCodex
	}
	return HarnessClaudeCode
}

// IsCheckpoint reports whether callIndex is an escalation check (calls 4, 7, 10, ...).
func IsCheckpoint(callIndex int) bool {
	return callIndex > 1 && (callIndex-1)%EscalationCadence == 0
}

func (r *Router) Route(ctx context.Context, req router.Request) (router.Decision, error) {
	lineup := Lineups[HarnessFor(req.ClientApp)]
	key := sessionKey(req)
	session := r.load(key)
	_, subscribed := req.SubscriptionProviders[lineup.Subscription]
	if subscribed {
		session.task = ChoiceHigh
	}
	callIndex := AgentCallIndex(req.ConversationMessages)
	checkpoint := IsCheckpoint(callIndex)
	ids := []QuestionID{}
	if !session.escalated {
		ids = append(ids, QuestionCallComplexity)
	}
	if session.task == "" {
		ids = append(ids, QuestionTaskComplexity)
	}
	if checkpoint && !session.escalated {
		ids = append(ids, QuestionEscalate)
	}
	answers := map[QuestionID]Answer{}
	if len(ids) > 0 {
		classified, err := r.classifier.Classify(ctx, BuildState(req.ConversationMessages), ids)
		if err != nil {
			return router.Decision{}, fmt.Errorf("%w: %w", ErrUnavailable, err)
		}
		answers = classified
	}
	if answer, found := answers[QuestionTaskComplexity]; found {
		session.task = answer.Choice
	}
	escalationEligible := session.task == ChoiceLow || session.fastSince
	if answer, found := answers[QuestionEscalate]; found && escalationEligible && answer.Choice == ChoiceYes {
		session.escalated = true
	}
	if checkpoint {
		session.fastSince = false
	}
	model := pickModel(lineup, session, answers[QuestionCallComplexity].Choice)
	decision, err := resolve(req, model, lineup)
	if err != nil {
		return router.Decision{}, err
	}
	if decision.Model == lineup.Fast {
		session.fastSince = true
	}
	r.store(key, session)
	decision.Reason = fmt.Sprintf("jev task=%s call=%s escalated=%t subscription=%t call_index=%d", session.task, answers[QuestionCallComplexity].Choice, session.escalated, subscribed, callIndex)
	return decision, nil
}

func pickModel(lineup Lineup, session sessionState, call Choice) string {
	switch {
	case session.escalated:
		return lineup.Top
	case call == ChoiceLow:
		return lineup.Fast
	case session.task == ChoiceHigh:
		return lineup.Top
	default:
		return lineup.LowTaskJudgment
	}
}

// resolve keeps the picked model when the request can dispatch it and otherwise
// falls back through the lineup from strongest to cheapest.
func resolve(req router.Request, model string, lineup Lineup) (router.Decision, error) {
	for _, candidate := range []string{model, lineup.Top, lineup.Fast, lineup.LowTaskJudgment} {
		if provider, ok := dispatchable(req, candidate); ok {
			return router.Decision{Provider: provider, Model: candidate}, nil
		}
	}
	return router.Decision{}, fmt.Errorf("%w: no lineup model is dispatchable", ErrUnavailable)
}

func dispatchable(req router.Request, model string) (string, bool) {
	for _, excluded := range []map[string]struct{}{req.ExcludedModels, req.SafetyExcludedModels, req.ContextWindowExcludedModels, req.UnsignedHistoryExcludedModels} {
		if _, found := excluded[model]; found {
			return "", false
		}
	}
	if _, allowed := req.AllowedModels[model]; len(req.AllowedModels) > 0 && !allowed {
		return "", false
	}
	entry, found := catalog.ByID(model)
	if !found {
		return "", false
	}
	for _, binding := range entry.Providers {
		if _, enabled := req.EnabledProviders[binding.Provider]; req.EnabledProviders == nil || enabled {
			return binding.Provider, true
		}
	}
	return "", false
}

func sessionKey(req router.Request) string {
	if req.FeedbackKey != "" {
		return req.FeedbackKey
	}
	return req.ClientSessionID
}

func (r *Router) load(key string) sessionState {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.sessions[key]
}

func (r *Router) store(key string, session sessionState) {
	if key == "" {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, known := r.sessions[key]; !known {
		r.order = append(r.order, key)
	}
	r.sessions[key] = session
	for len(r.order) > maxTrackedSessions {
		delete(r.sessions, r.order[0])
		r.order = r.order[1:]
	}
}
