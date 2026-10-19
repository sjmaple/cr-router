package jev

import "weave-os/router/internal/router"

const (
	maxUserMessages  = 3
	maxAgentTurns    = 6
	maxObjectiveRune = 2000
	maxUserRune      = 1000
	maxAgentRune     = 500
)

// ToolCallStatus is a tool call's outcome without its output.
type ToolCallStatus string

const (
	ToolCallOK      ToolCallStatus = "ok"
	ToolCallError   ToolCallStatus = "error"
	ToolCallPending ToolCallStatus = "pending"
)

// ToolCall names a call and its status; outputs are deliberately omitted.
type ToolCall struct {
	Name   string         `json:"name"`
	Status ToolCallStatus `json:"status"`
}

// AgentTurn is one assistant turn reduced to its text and tool statuses.
type AgentTurn struct {
	Text      string     `json:"text,omitempty"`
	ToolCalls []ToolCall `json:"tool_calls,omitempty"`
}

// State is the compact conversation the judge sees: the objective, the last
// three user messages, and the last six agent turns.
type State struct {
	Objective          string      `json:"objective"`
	RecentUserMessages []string    `json:"recent_user_messages"`
	RecentAgentTurns   []AgentTurn `json:"recent_agent_turns"`
}

func isHumanMessage(message router.ConversationMessage) bool {
	return message.Role == "user" && message.Text != ""
}

// BuildState keeps the first human message as the objective so short
// follow-ups like "continue" never hide what the task is.
func BuildState(messages []router.ConversationMessage) State {
	state := State{RecentUserMessages: []string{}, RecentAgentTurns: []AgentTurn{}}
	for index, message := range messages {
		switch {
		case isHumanMessage(message) && state.Objective == "":
			state.Objective = truncate(message.Text, maxObjectiveRune)
		case isHumanMessage(message):
			state.RecentUserMessages = appendBounded(state.RecentUserMessages, truncate(message.Text, maxUserRune), maxUserMessages)
		case message.Role == "assistant":
			state.RecentAgentTurns = appendBounded(state.RecentAgentTurns, agentTurn(message, messages[index+1:]), maxAgentTurns)
		}
	}
	return state
}

func agentTurn(message router.ConversationMessage, following []router.ConversationMessage) AgentTurn {
	turn := AgentTurn{Text: truncate(message.Text, maxAgentRune)}
	var results []router.ConversationToolResult
	if len(following) > 0 && following[0].Role != "assistant" {
		results = following[0].ToolResults
	}
	for index, call := range message.ToolCalls {
		status := ToolCallPending
		if index < len(results) && results[index].IsError {
			status = ToolCallError
		} else if index < len(results) {
			status = ToolCallOK
		}
		turn.ToolCalls = append(turn.ToolCalls, ToolCall{Name: call.Name, Status: status})
	}
	return turn
}

// AgentCallIndex is the 1-based logical model call this request starts.
func AgentCallIndex(messages []router.ConversationMessage) int {
	calls := 1
	for _, message := range messages {
		if message.Role == "assistant" {
			calls++
		}
	}
	return calls
}

func appendBounded[T any](items []T, item T, limit int) []T {
	items = append(items, item)
	if len(items) > limit {
		items = items[len(items)-limit:]
	}
	return items
}

func truncate(text string, limit int) string {
	runes := []rune(text)
	if len(runes) <= limit {
		return text
	}
	return string(runes[:limit])
}
