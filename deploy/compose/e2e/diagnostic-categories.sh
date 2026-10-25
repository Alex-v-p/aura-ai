#!/usr/bin/env bash

# Convert bounded source-log input into a finite, metadata-only diagnostic
# summary.  This deliberately never emits a source line or a source value.
classify_diagnostic_stream() {
  awk '
    function emit(name, value) {
      if (value > 0) {
        if (emitted++) printf ","
        printf "%s:%d", name, value
      }
    }
    {
      line = tolower($0)
      if (line ~ /migration/) migration++
      if (line ~ /database|postgres|relation/) database++
      if (line ~ /clickhouse/) clickhouse++
      if (line ~ /connection refused|timeout|unavailable/) dependency_unavailable++
      if (line ~ /permission|forbidden|unauthorized/) authorization++
      if (line ~ /exception|traceback/) exception++
      if (line ~ /resource.limit.exceeded/) resource_limit++
      # Runner signals are finite enum/metric names, not source messages. Keep
      # them visible so a startup/ledger/transport failure can be distinguished
      # without retaining log content or credentials.
      if (line ~ /request_invalid/) request_invalid++
      if (line ~ /resource_limit_exceeded/) resource_limit_exceeded++
      if (line ~ /dependency_unavailable/) dependency_unavailable_code++
      if (line ~ /internal_failure/) internal_failure++
      if (line ~ /terminal_settlement_failure/) terminal_settlement_failure++
      if (line ~ /terminal_accounting_failure/) terminal_accounting_failure++
      if (line ~ /publication_recovery_failure/) publication_recovery_failure++
      if (line ~ /nats_(fetch|publish|drain)_failure/) nats_transport_failure++
      if (line ~ /error|failed/) failure++
      if (line ~ /provider|ollama|judge/) provider++
    }
    END {
      printf "categories="
      emit("migration", migration)
      emit("database", database)
      emit("clickhouse", clickhouse)
      emit("dependency_unavailable", dependency_unavailable)
      emit("authorization", authorization)
      emit("exception", exception)
      emit("resource_limit", resource_limit)
      emit("request_invalid", request_invalid)
      emit("resource_limit_exceeded", resource_limit_exceeded)
      emit("dependency_unavailable_code", dependency_unavailable_code)
      emit("internal_failure", internal_failure)
      emit("terminal_settlement_failure", terminal_settlement_failure)
      emit("terminal_accounting_failure", terminal_accounting_failure)
      emit("publication_recovery_failure", publication_recovery_failure)
      emit("nats_transport_failure", nats_transport_failure)
      emit("failure", failure)
      emit("provider", provider)
      if (!emitted) printf "none"
      print ""
    }
  '
}
