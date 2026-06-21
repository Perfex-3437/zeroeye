#!/usr/bin/env ruby
# frozen_string_literal: true

#
# BackoffCalculator  —  Testable exponential backoff helper
#
# Extracted from MarketStreamClient#schedule_reconnect so the backoff
# calculation can be validated without opening real network connections
# or running an EventMachine reactor.
#
# Usage:
#   calc = BackoffCalculator.new(base_delay: 1, max_delay: 120)
#   calc.delay_for(0)  # => 1
#   calc.delay_for(1)  # => 2
#   calc.delay_for(7)  # => 120   (capped)
#

module BackoffCalculator
  # Returns the backoff delay (in seconds) for a given attempt number.
  #
  #   attempt  — zero-based reconnection attempt counter
  #   base     — initial delay in seconds (default: 1)
  #   max      — maximum delay cap in seconds (default: 120)
  #
  # Formula:  min(base * 2 ** attempt, max)
  #
  # Known behaviour (preserved from the original for compatibility):
  #   attempt=0 → base * 1  (i.e. first retry is `base` seconds)
  #   attempt=1 → base * 2
  #   attempt=2 → base * 4
  #   ...
  def self.delay_for(attempt, base: 1, max: 120)
    [(base * (2 ** attempt)), max].min
  end
end
