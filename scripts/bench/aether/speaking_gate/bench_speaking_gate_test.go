package webrtc

import (
	"encoding/json"
	"os"
	"strconv"
	"sync"
	"testing"
	"time"
)

const (
	gateSentenceBytes = 16000
	gateChunkBytes    = 4096
	gateSentenceDur   = time.Second
)

type gateTransition struct {
	AtMs     float64 `json:"at_ms"`
	Speaking bool    `json:"speaking"`
}

type gateResult struct {
	GapMs                  int              `json:"gap_ms"`
	Rep                    int              `json:"rep"`
	UnmutedMidReply        bool             `json:"unmuted_mid_reply"`
	MidReplyOpenMs         float64          `json:"mid_reply_open_ms"`
	ReleaseAfterDeliveryMs float64          `json:"release_after_delivery_ms"`
	Transitions            []gateTransition `json:"transitions"`
}

func toMs(d time.Duration) float64 {
	return float64(d.Microseconds()) / 1000.0
}

func runGate(t *testing.T, gap time.Duration, rep int) gateResult {
	session, err := NewPeerSession(NewEngine(), "bench-gate", func(interface{}) error { return nil })
	if err != nil {
		t.Fatalf("NewPeerSession: %v", err)
	}
	defer session.Close()

	t0 := time.Now()
	stop := make(chan struct{})
	var mu sync.Mutex
	var transitions []gateTransition
	go func() {
		last := false
		for {
			select {
			case <-stop:
				return
			default:
			}
			v := session.AgentSpeaking.Load()
			if v != last {
				mu.Lock()
				transitions = append(transitions, gateTransition{toMs(time.Since(t0)), v})
				mu.Unlock()
				last = v
			}
			time.Sleep(time.Millisecond)
		}
	}()

	deliver := func() {
		for off := 0; off < gateSentenceBytes; off += gateChunkBytes {
			end := min(off+gateChunkBytes, gateSentenceBytes)
			session.PCMOutboundChan <- make([]byte, end-off)
		}
	}

	waitFor := func(want bool, timeout time.Duration) (time.Duration, bool) {
		deadline := time.Now().Add(timeout)
		for time.Now().Before(deadline) {
			if session.AgentSpeaking.Load() == want {
				return time.Since(t0), true
			}
			time.Sleep(time.Millisecond)
		}
		return 0, false
	}

	deliver()
	firstTrue, ok := waitFor(true, 2*time.Second)
	if !ok {
		close(stop)
		t.Fatalf("gap %v: AgentSpeaking never became true", gap)
	}

	secondDeliverAt := firstTrue + gateSentenceDur + gap
	time.Sleep(time.Until(t0.Add(secondDeliverAt)))
	deliver()
	deliveredAt := time.Since(t0)

	waitFor(true, time.Second)
	finalFalseAt, ok := waitFor(false, 6*time.Second)
	time.Sleep(50 * time.Millisecond)
	close(stop)

	mu.Lock()
	defer mu.Unlock()
	res := gateResult{GapMs: int(gap / time.Millisecond), Rep: rep, Transitions: transitions}

	secondDeliverMs := toMs(secondDeliverAt)
	var openedAt float64 = -1
	for _, tr := range transitions {
		if !tr.Speaking && tr.AtMs < secondDeliverMs {
			res.UnmutedMidReply = true
			openedAt = tr.AtMs
		}
		if tr.Speaking && openedAt >= 0 && tr.AtMs >= openedAt {
			res.MidReplyOpenMs = tr.AtMs - openedAt
			openedAt = -1
		}
	}
	if ok {
		res.ReleaseAfterDeliveryMs = toMs(finalFalseAt) - toMs(deliveredAt)
	}
	return res
}

func TestSpeakingGateSweep(t *testing.T) {
	reps := 5
	if v := os.Getenv("AETHER_BENCH_REPS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			reps = n
		}
	}

	gaps := []int{100, 250, 350, 450, 800, 1500}
	var results []gateResult
	for _, g := range gaps {
		for r := 1; r <= reps; r++ {
			res := runGate(t, time.Duration(g)*time.Millisecond, r)
			results = append(results, res)
			t.Logf("gap=%4dms rep=%d unmuted_mid_reply=%v open=%.0fms release_after_delivery=%.0fms",
				res.GapMs, res.Rep, res.UnmutedMidReply, res.MidReplyOpenMs, res.ReleaseAfterDeliveryMs)
		}
	}

	if out := os.Getenv("AETHER_BENCH_OUT"); out != "" {
		data, err := json.MarshalIndent(results, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(out, data, 0o644); err != nil {
			t.Fatal(err)
		}
	}
}
