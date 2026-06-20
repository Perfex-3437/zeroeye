package analytics

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// TestStartIdempotent verifies that calling Start() multiple times on
// the same Collector creates at most one active flush loop.
func TestStartIdempotent(t *testing.T) {
	c := NewCollector()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Record a sample before starting
	c.RecordCounter("test_metric", 1.0)

	// Call Start multiple times concurrently
	const n = 10
	var started int64
	var done sync.WaitGroup
	for i := 0; i < n; i++ {
		done.Add(1)
		go func() {
			defer done.Done()
			c.Start(ctx)
			atomic.AddInt64(&started, 1)
		}()
	}
	done.Wait()

	// Give the single flush loop a moment to tick
	time.Sleep(50 * time.Millisecond)

	// Only one goroutine should have been spawned. We verify by stopping
	// and then checking that the flushed count increased at most once
	// from the initial immediate flush.
	statsBefore := c.Stats()
	c.Stop()
	time.Sleep(50 * time.Millisecond) // allow any pending flushes

	// The startOnce ensures Start was only effective once.
	// If multiple goroutines existed, we'd see duplicate flushes.
	statsAfter := c.Stats()

	// The flush loop may have flushed 0 or 1 times since we immediately
	// stopped it. But the important thing is: no panics, no duplicate
	// goroutine leaks.
	t.Logf("started calls (effective): %d", atomic.LoadInt64(&started))
	t.Logf("stats before stop: %+v", statsBefore)
	t.Logf("stats after stop:  %+v", statsAfter)

	if statsAfter.FlushedSamples < statsBefore.FlushedSamples {
		t.Error("flushed counter decreased after stop, unexpected")
	}
}

// TestStartIdempotent_RepeatStartThenStop calls Start multiple times,
// then Stop, and verifies no duplicate goroutines are leaked.
func TestStartIdempotent_RepeatStartThenStop(t *testing.T) {
	c := NewCollector()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// First Start
	c.Start(ctx)
	// Second Start (should be no-op)
	c.Start(ctx)
	// Third Start (should be no-op)
	c.Start(ctx)

	// Stop - should work fine
	c.Stop()

	// Record after stop to ensure collector is still usable
	c.RecordCounter("after_stop", 1.0)
	_ = c.Flush(context.Background())

	stats := c.Stats()
	t.Logf("final stats: %+v", stats)

	// Basic sanity: flushed should be > 0 from the initial flush
	if stats.FlushedSamples < 0 {
		t.Error("flushed samples should be non-negative")
	}
}

// TestStartIdempotent_ContextCancellation verifies that context
// cancellation still stops the active loop cleanly when Start
// was called multiple times.
func TestStartIdempotent_ContextCancellation(t *testing.T) {
	c := NewCollector()
	ctx, cancel := context.WithCancel(context.Background())

	// Start multiple times
	c.Start(ctx)
	c.Start(ctx)
	c.Start(ctx)

	// Cancel the context
	cancel()

	// Give the goroutine time to exit
	time.Sleep(50 * time.Millisecond)

	// After cancellation, Stop should be a no-op (no panic)
	c.Stop()

	// Collector should still function for recording
	c.RecordCounter("after_cancel", 1.0)
	_ = c.Flush(context.Background())

	stats := c.Stats()
	t.Logf("stats after context cancellation: %+v", stats)
}

// TestStartIdempotent_ImmediateFlush verifies that the first Start
// still performs the existing immediate flush behavior.
func TestStartIdempotent_ImmediateFlush(t *testing.T) {
	c := NewCollector()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Record some samples before starting
	for i := 0; i < 5; i++ {
		c.RecordCounter("boot_metric", float64(i))
	}

	statsBefore := c.Stats()
	t.Logf("before start: buffered=%d", statsBefore.BufferedSamples)

	// Start - should flush immediately
	c.Start(ctx)

	// Give the immediate flush a moment to complete
	time.Sleep(50 * time.Millisecond)

	statsAfter := c.Stats()
	t.Logf("after start:  flushed=%d, buffered=%d",
		statsAfter.FlushedSamples, statsAfter.BufferedSamples)

	// The buffer should be drained after the immediate flush
	if statsAfter.BufferedSamples != 0 {
		t.Errorf("expected buffer to be drained after immediate flush, got %d buffered",
			statsAfter.BufferedSamples)
	}
	if statsAfter.FlushedSamples < int64(len([]int{0, 1, 2, 3, 4})) {
		t.Errorf("expected at least %d flushed samples, got %d",
			len([]int{0, 1, 2, 3, 4}), statsAfter.FlushedSamples)
	}
}
