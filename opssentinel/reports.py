"""Readable incident evidence, without exporting raw logs or arbitrary model context."""
from __future__ import annotations

from datetime import datetime
import html
import re


def text(value) -> str:
    value = html.escape(str(value if value is not None else "—"), quote=False)
    value = re.sub(r"[\r\n\t]+", " ", value)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", value)


def incident_report(incident: dict, actions: list[dict], events: list[dict]) -> str:
    status = incident['status']
    kind = incident.get('resolution_kind')
    conclusion = ("业务恢复已由后续探测确认；本系统动作成功返回。" if status == 'resolved' and kind == 'mitigated'
                  else "业务恢复已由后续探测确认，不能归因于本系统动作。" if status == 'resolved'
                  else "尚未确认业务恢复。")
    lines = ['# OpsSentinel 事故报告', '', f"服务：{text(incident.get('service_name'))}", '',
             f"事故：{text(incident['id'])}", '', f"状态：{text(status)}", '', conclusion, '',
             '本报告记录观测与处置结果，不证明源代码根因已消除。原始日志与任意上下文字段不包含在此摘要中。', '',
             f"发现时间：{text(incident.get('created_at'))}", '', f"恢复时间：{text(incident.get('resolved_at'))}", '']
    if incident.get('resolved_at'):
        elapsed = (datetime.fromisoformat(incident['resolved_at']) - datetime.fromisoformat(incident['created_at'])).total_seconds()
        lines += [f'发现至确认恢复：{max(0, elapsed):.1f} 秒（不是故障实际开始时间）', '']
    lines += ['## 诊断', '', text(incident.get('diagnosis')), '', '## 执行记录', '',
              '| 操作 ID | 动作 | 持久化状态 | 结果摘要 |', '| --- | --- | --- | --- |']
    for action in actions:
        lines.append('| ' + ' | '.join(text(v) for v in (action['id'], action['action'], action['status'], action['document'].get('summary'))) + ' |')
    if not actions:
        lines.append('| — | — | 未执行 | 无动作记录 |')
    evidence = incident.get('evidence', {})
    snapshots = [('初始观测', evidence.get('initial'))]
    snapshots += [(f'第 {n} 次动作前观测', s) for n, s in enumerate(evidence.get('before_actions', []), 1)]
    snapshots.append(('恢复确认观测', evidence.get('recovery')))
    for title, snapshot in snapshots:
        lines += ['', '## ' + title, '']
        if not snapshot:
            lines.append('无记录。')
            continue
        lines += [f"观测时间：{text(snapshot.get('observed_at'))}", '', text(snapshot.get('summary')), '',
                  '| 检查 | 结果 | 证据 |', '| --- | --- | --- |']
        for check in snapshot.get('checks', []):
            lines.append('| ' + ' | '.join(text(v) for v in (check.get('name'), '通过' if check.get('ok') is True else '未通过', check.get('detail'))) + ' |')
    lines += ['', '## 事件时间线', '', '| 时间 | 事件 | 记录 |', '| --- | --- | --- |']
    for event in events:
        lines.append('| ' + ' | '.join(text(event.get(k)) for k in ('created_at', 'kind', 'message')) + ' |')
    return '\n'.join(lines) + '\n'
