package jev

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestStaleStoreKeepsEscalationAndTask(t *testing.T) {
	jevRouter := New(nil)
	jevRouter.store("session", sessionState{taskComplexity: ChoiceLow, escalated: true})
	jevRouter.store("session", sessionState{taskComplexity: ChoiceHigh})
	require.Equal(t, sessionState{taskComplexity: ChoiceLow, escalated: true}, jevRouter.load("session"))
}
