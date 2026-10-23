#!/bin/sh
set -eu

# Generate the NATS authorization file in tmpfs. Credentials are read only
# from Docker secrets and are never passed on a command line or printed.
read_secret() {
  value=$(cat "$1")
  case "$value" in
    ""|*' '*|*'	'*|*'\n'*|*'\r'*)
      echo 'NATS secret values must be non-empty single-line values' >&2
      exit 78
      ;;
  esac
  printf '%s' "$value"
}

validate_name() {
  case "$1" in
    ''|*[!A-Za-z0-9._-]*) echo 'NATS user names must be bounded identifiers' >&2; exit 78 ;;
  esac
}

normal_user=${AURA_NATS_USER:-aura}
bootstrap_user=${AURA_NATS_BOOTSTRAP_USER:-aura-evaluation-bootstrap}
dispatch_user=${AURA_NATS_DISPATCH_USER:-aura-evaluation-dispatch}
runner_user=${AURA_NATS_RUNNER_USER:-aura-evaluation-runner}
validate_name "$normal_user"
validate_name "$bootstrap_user"
validate_name "$dispatch_user"
validate_name "$runner_user"

normal_password=$(read_secret /run/secrets/aura-nats-password)
bootstrap_password=$(read_secret /run/secrets/aura-nats-bootstrap-password)
dispatch_password=$(read_secret /run/secrets/aura-nats-dispatch-password)
runner_password=$(read_secret /run/secrets/aura-nats-runner-password)

config=$(mktemp /tmp/aura-nats.XXXXXX)
chmod 0600 "$config"
cleanup() { rm -f "$config"; }
trap cleanup EXIT INT TERM

cat >"$config" <<EOF
port: 4222
http_port: 8222
jetstream {
  store_dir: /data
}

authorization {
  users: [
    {
      user: "$normal_user"
      password: "$normal_password"
      permissions: {
        publish: {
          allow: ["aura.runs.>", "aura.approvals.>", "aura.notifications.>", "aura.artifacts.>", "aura.runs.resume.v1", "aura.memory.process.v1", "aura.automation.reminders.v1", "aura.automation.notifications.v1", "aura.knowledge.documents.process.v1", "aura.knowledge.documents.delete.v1", "aura.knowledge.documents.events.v1", "\$JS.API.INFO", "\$JS.API.STREAM.LIST", "\$JS.API.STREAM.INFO.AURA_RUNS", "\$JS.API.STREAM.CREATE.AURA_RUNS", "\$JS.API.STREAM.UPDATE.AURA_RUNS", "\$JS.API.CONSUMER.INFO.AURA_RUNS.aura-core-worker", "\$JS.API.CONSUMER.CREATE.AURA_RUNS.aura-core-worker.aura.runs.execute.v1", "\$JS.API.CONSUMER.MSG.NEXT.AURA_RUNS.aura-core-worker", "\$JS.ACK.AURA_RUNS.aura-core-worker.>", "\$JS.API.CONSUMER.INFO.AURA_RUNS.aura-core-memory-worker", "\$JS.API.CONSUMER.CREATE.AURA_RUNS.aura-core-memory-worker.aura.memory.process.v1", "\$JS.API.CONSUMER.MSG.NEXT.AURA_RUNS.aura-core-memory-worker", "\$JS.ACK.AURA_RUNS.aura-core-memory-worker.>", "\$JS.API.CONSUMER.INFO.AURA_RUNS.aura-core-approval-resume-worker", "\$JS.API.CONSUMER.CREATE.AURA_RUNS.aura-core-approval-resume-worker.aura.runs.resume.v1", "\$JS.API.CONSUMER.MSG.NEXT.AURA_RUNS.aura-core-approval-resume-worker", "\$JS.ACK.AURA_RUNS.aura-core-approval-resume-worker.>", "\$JS.API.CONSUMER.INFO.AURA_RUNS.aura-core-document-process-worker", "\$JS.API.CONSUMER.CREATE.AURA_RUNS.aura-core-document-process-worker.aura.knowledge.documents.process.v1", "\$JS.API.CONSUMER.MSG.NEXT.AURA_RUNS.aura-core-document-process-worker", "\$JS.ACK.AURA_RUNS.aura-core-document-process-worker.>", "\$JS.API.CONSUMER.INFO.AURA_RUNS.aura-core-document-delete-worker", "\$JS.API.CONSUMER.CREATE.AURA_RUNS.aura-core-document-delete-worker.aura.knowledge.documents.delete.v1", "\$JS.API.CONSUMER.MSG.NEXT.AURA_RUNS.aura-core-document-delete-worker", "\$JS.ACK.AURA_RUNS.aura-core-document-delete-worker.>"]
          deny: ["aura.evaluation.>"]
        }
        subscribe: {
          allow: ["aura.runs.>", "aura.approvals.>", "aura.notifications.>", "aura.artifacts.>", "aura.memory.process.v1", "aura.knowledge.documents.process.v1", "aura.knowledge.documents.delete.v1", "_INBOX.>"]
          deny: ["aura.evaluation.>"]
        }
      }
    }
    {
      user: "$bootstrap_user"
      password: "$bootstrap_password"
      permissions: {
        publish: { allow: ["\$JS.API.STREAM.INFO.AURA_EVALUATION_RUNNER", "\$JS.API.STREAM.CREATE.AURA_EVALUATION_RUNNER", "\$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.aura-evaluation-dispatch", "\$JS.API.CONSUMER.DURABLE.CREATE.AURA_EVALUATION_RUNNER.aura-evaluation-dispatch", "\$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.aura-core-evaluation-runner", "\$JS.API.CONSUMER.DURABLE.CREATE.AURA_EVALUATION_RUNNER.aura-core-evaluation-runner", "\$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.aura-core-evaluation-cancellation", "\$JS.API.CONSUMER.DURABLE.CREATE.AURA_EVALUATION_RUNNER.aura-core-evaluation-cancellation"] }
        subscribe: { allow: ["_INBOX.>"] }
      }
    }
    {
      user: "$dispatch_user"
      password: "$dispatch_password"
      permissions: {
        publish: { allow: ["aura.evaluation.runner.requested.v1", "aura.evaluation.runner.cancellation.v1", "\$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.aura-evaluation-dispatch", "\$JS.API.CONSUMER.MSG.NEXT.AURA_EVALUATION_RUNNER.aura-evaluation-dispatch", "\$JS.ACK.AURA_EVALUATION_RUNNER.aura-evaluation-dispatch.>"] }
        subscribe: { allow: ["aura.evaluation.runner.results.v1", "_INBOX.>"] }
      }
    }
    {
      user: "$runner_user"
      password: "$runner_password"
      permissions: {
        publish: { allow: ["aura.evaluation.runner.results.v1", "\$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.aura-core-evaluation-runner", "\$JS.API.CONSUMER.MSG.NEXT.AURA_EVALUATION_RUNNER.aura-core-evaluation-runner", "\$JS.API.CONSUMER.INFO.AURA_EVALUATION_RUNNER.aura-core-evaluation-cancellation", "\$JS.API.CONSUMER.MSG.NEXT.AURA_EVALUATION_RUNNER.aura-core-evaluation-cancellation", "\$JS.ACK.AURA_EVALUATION_RUNNER.aura-core-evaluation-runner.>", "\$JS.ACK.AURA_EVALUATION_RUNNER.aura-core-evaluation-cancellation.>"] }
        subscribe: { allow: ["aura.evaluation.runner.requested.v1", "aura.evaluation.runner.cancellation.v1", "_INBOX.>"] }
      }
    }
  ]
}
EOF

unset normal_password bootstrap_password dispatch_password runner_password
exec nats-server -c "$config"
