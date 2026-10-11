"""Official CLI command hook. Failure emits a blocking result, never an empty success."""
import json
import sys

from .client import call


def run(payload, invoke=call):
    event = payload.get('hook_event_name')
    try:
        if event == 'PreToolUse':
            invoke('guard', {})
            # A valid task is not blanket approval for the proposed command.
            return {}
        if event == 'SessionStart' and payload.get('source') == 'startup':
            # Fresh CLI runs also fire UserPromptSubmit before their first model
            # request. Validate here, inject the full current text at that prompt.
            # No delivery receipt is minted, so tools stay blocked until injection.
            if not invoke('preflight', {}).get('complete'):
                raise RuntimeError('Full startup rules unavailable')
            return {}
        result = invoke('rules', {'event': event, 'session': payload.get('session_id')})
        if not result.get('text'):
            raise RuntimeError('Full rules absent')
        if event == 'PostCompact':
            # Codex ignores context on this event. SessionStart(compact) performs
            # actual injection; this event only verifies that reload is possible.
            return {}
        return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': result['text']}}
    except Exception:
        reason = 'Loom 当前完整规则或权限无法核验；本轮已停止，请检查受控运行日志。'
        if event == 'PreToolUse':
            return {'hookSpecificOutput': {'hookEventName': event, 'permissionDecision': 'deny', 'permissionDecisionReason': reason}}
        return {'continue': False, 'stopReason': reason}


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    print(json.dumps(run(payload), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
