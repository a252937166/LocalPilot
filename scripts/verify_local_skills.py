"""Local skill discovery, lazy reads and project edits over actual MCP stdio."""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

from mcp import Client, StdioServerParameters


async def verify(output):
    source = Path(__file__).resolve().parents[1]
    checks = []
    def check(name, value):
        checks.append({'name': name, 'passed': bool(value)})
        assert value, name
    async def call(client, name, **args):
        result = await client.call_tool(name, args)
        assert not result.is_error, (name, result.content)
        return result.structured_content
    async def rejected(client, name, **args):
        return (await client.call_tool(name, args)).is_error
    def write(path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    def skill(path, name, description, body='BODY_ONLY_ON_DEMAND', extra=''):
        write(path / 'SKILL.md', f'---\nname: {name}\ndescription: {description}\n{extra}---\n{body}\n')
        return path / 'SKILL.md'
    with tempfile.TemporaryDirectory(prefix='localpilot-skills-') as directory:
        base = Path(directory).resolve(); work = base/'project'; work.mkdir()
        (work/'.git').mkdir(); (work/'module').mkdir()
        write(work/'AGENTS.md', 'ROOT_GUIDANCE')
        write(work/'module/AGENTS.md', 'OLD_MODULE_RULE')
        write(work/'module/AGENTS.override.md', 'MODULE_OVERRIDE')
        write(work/'other/CLAUDE.md', 'OTHER_MODULE_RULE')
        write(work/'main.py', 'value = 1\n')
        primary = skill(work/'.agents/skills/money', 'money', 'Use for money arithmetic and rounding.',
                        'Read references/contract.md before editing.\n' + 'FULL_BODY_MARKER ' * 3000)
        write(primary.parent/'references/contract.md', 'EXPECTED_REFERENCE_BODY')
        write(primary.parent/'scripts/check.py', 'print("SCRIPT_EXECUTED_ONLY_EXPLICITLY")')
        nested = skill(work/'module/.agents/skills/money', 'money', 'Module-only money rules.')
        foreign = skill(work/'other/.agents/skills/foreign', 'foreign', 'Other subtree only.')
        skill(work/'skills/manual', 'manual', 'Explicit invocation only.', extra='disable-model-invocation: true\n')
        policy = skill(work/'.agents/skills/policy', 'policy', 'Explicit policy only.')
        write(policy.parent/'agents/openai.yaml', 'policy:\n  allow_implicit_invocation: false\n')
        skill(work/'.agents/skills/unrelated', 'banana', 'Banana harvesting.', body='UNRELATED_BODY_PRIVATE')
        skill(work/'.agents/skills/unrelated/workspace/generated', 'generated', 'Generated workspace fixture.')
        write(work/'.agents/skills/bad/SKILL.md', '---\nname: bad\ndescription: [not, scalar]\n---\nbad')
        write(work/'.agents/skills/huge/SKILL.md', 'x' * (128*1024 + 1))
        outside = skill(base/'external/linked', 'linked', 'Linked fixture outside workspace.')
        (work/'.agents/skills/link').symlink_to(outside.parent)
        (work/'.agents/skills/loop').symlink_to(work/'.agents/skills')
        (primary.parent/'references/escape.md').symlink_to(work/'AGENTS.md')
        # Ranking fixtures: a Chinese trigger phrase must beat loose single-character overlap.
        km = skill(work/'.agents/skills/km-docs', 'km-docs', '团队知识库文档工具，读取和编辑团队知识库页面。激活方式：提到团队知识库/km/wiki/知识库时使用。')
        skill(work/'.agents/skills/city-notes', 'city-notes', '整理城市学习笔记与资料归档。')
        home = base/'home'; (home/'.claude/skills').mkdir(parents=True)
        citadel = skill(home/'.claude/skills/citadel', 'citadel', '团队知识库 km/wiki 官方工具。激活方式：遇到 docs.example.com 链接或提到团队知识库/文档/collabpage 时优先激活。')
        write(home/'AGENTS.md', '# AGENTS\n\n团队知识库访问使用 `docs-cli workspace`。\n')
        state, config = base/'state', base/'config/config.json'
        write(config, json.dumps({'workspaces': {'project': str(work)}, 'state_dir': str(state),
                                 'local_skill_roots': [], 'max_file_bytes': 2*1024*1024}))
        args = StdioServerParameters(command=sys.executable, args=['-I', str(source/'agent/server.py')],
                                     env={'LOCALPILOT_CONFIG': str(config), 'HOME': str(home)})
        async with Client(args) as client:
            toolset = {t.name: t for t in (await client.list_tools()).tools}
            check('read_only_discovery_and_loader', all(toolset[n].annotations.read_only_hint for n in ('get_project_context', 'read_local_skill')))
            status = await call(client, 'device_status')
            check('device_discovers_skill_tools', status['local_skills']['read_tool'] == 'read_local_skill')
            context = await call(client, 'get_project_context', workspace='project', path='module', query='money')
            paths = [s['skill_path'] for s in context['skills']]
            check('nearest_module_skill_precedes_same_name_parent', paths.index(str(nested)) < paths.index(str(primary)))
            check('foreign_subtree_not_injected', str(foreign) not in paths and len([s for s in context['skills'] if s['name'] == 'money']) == 2)
            check('parent_to_child_rules_and_override', [d['content'] for d in context['instructions']] == ['ROOT_GUIDANCE', 'MODULE_OVERRIDE'])
            serialized = json.dumps(context)
            check('metadata_only_no_eager_body', 'FULL_BODY_MARKER' not in serialized and 'UNRELATED_BODY_PRIVATE' not in serialized)
            check('manual_invocation_flags', all(not s['allow_implicit_invocation'] for s in context['skills'] if s['name'] in ('policy', 'manual')))
            check('invalid_and_oversize_reported', len([e for e in context['errors'] if 'AGENTS.md' not in e['path']]) == 2 and any('AGENTS.md' in e['path'] for e in context['errors']))
            check('workspace_external_symlink_not_loaded', str(outside) not in paths)
            check('generated_skill_workspace_not_scanned', all(s['name'] != 'generated' for s in context['skills']))
            check('missing_scope_rejected', await rejected(client, 'get_project_context', workspace='project', path='missing/deeper/file.py'))
            check('outside_scope_rejected', await rejected(client, 'get_project_context', workspace='project', path=str(base/'external')))
            loaded = await call(client, 'read_local_skill', workspace='project', skill_path=str(primary), project_path='module', max_chars=1000)
            check('real_skill_page_and_hash', 'FULL_BODY_MARKER' in loaded['content'] and loaded['next_offset'] == 1000 and loaded['sha256'] == hashlib.sha256(primary.read_bytes()).hexdigest())
            tail = await call(client, 'read_local_skill', workspace='project', skill_path=str(primary), project_path='module', offset=1000, max_chars=64000, expected_sha256=loaded['sha256'])
            check('long_skill_fully_readable', tail['next_offset'] is None and len(loaded['content'] + tail['content']) == loaded['total_chars'])
            reference = await call(client, 'read_local_skill', workspace='project', skill_path=str(primary), project_path='module', resource_path='references/contract.md')
            check('reference_relative_to_skill_not_project', reference['content'] == 'EXPECTED_REFERENCE_BODY' and reference['base_dir'] == str(primary.parent))
            script = await call(client, 'read_local_skill', workspace='project', skill_path=str(primary), resource_path='scripts/check.py')
            with sqlite3.connect(state/'localpilot.sqlite3') as db:
                check('reading_scripts_never_executes', 'print(' in script['content'] and db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0)
                events = db.execute("SELECT details FROM events WHERE tool='read_local_skill'").fetchall()
                check('audits_paths_hashes_without_bodies', len(events) == 4 and all('FULL_BODY_MARKER' not in row[0] for row in events))
            check('reference_traversal_rejected', await rejected(client, 'read_local_skill', workspace='project', skill_path=str(primary), resource_path='../money/SKILL.md'))
            check('reference_absolute_rejected', await rejected(client, 'read_local_skill', workspace='project', skill_path=str(primary), resource_path=str(work/'AGENTS.md')))
            check('foreign_skill_rejected', await rejected(client, 'read_local_skill', workspace='project', skill_path=str(foreign), project_path='module'))
            check('fake_skill_path_rejected', await rejected(client, 'read_local_skill', workspace='project', skill_path=str(work/'main.py')))
            primary.write_text(primary.read_text() + '\nCHANGED')
            check('changed_skill_page_hash_rejected', await rejected(client, 'read_local_skill', workspace='project', skill_path=str(primary), expected_sha256=loaded['sha256']))
            refresh = await call(client, 'get_project_context', workspace='project', path='module')
            check('skill_changes_discovered_without_restart', next(s['sha256'] for s in refresh['skills'] if s['skill_path'] == str(primary)) != loaded['sha256'])
            other = await call(client, 'get_project_context', workspace='project', path='other')
            check('other_project_context_isolated', str(foreign) in [s['skill_path'] for s in other['skills']] and str(nested) not in [s['skill_path'] for s in other['skills']])
            check('claude_compatibility_rule', other['instructions'][-1]['content'] == 'OTHER_MODULE_RULE')
            for name, kw in [('read_file', {'path': 'main.py'}), ('list_directory', {'path': '.'}), ('search_files', {'path': '.', 'pattern': 'main.py'})]:
                entry = await call(client, name, workspace='project', **kw)
                check(name+'_project_discovery_hint', entry['project_context']['tool'] == 'get_project_context')
            task = await call(client, 'create_task', workspace='project', objective='Load a skill then update main.py and verify.',
                              plan=[{'id': 'edit', 'step': 'Edit following local guidance', 'status': 'in_progress'}],
                              checks=[{'kind': 'file_equals', 'path': 'main.py', 'value': 'value = 2\n'}])
            task_context = await call(client, 'inspect_task_step', task_id=task['task_id'], step_id='edit', operation='get_project_context', arguments={'path': '.'}, action_id='context')
            task_read = await call(client, 'inspect_task_step', task_id=task['task_id'], step_id='edit', operation='read_local_skill', arguments={'skill_path': str(primary), 'resource_path': 'references/contract.md'}, action_id='skill')
            check('harness_reads_do_not_spend_mutation_budget', task_read['task']['action_count'] == 0 and task_read['task']['observation_count'] == 2 and task_read['action']['result']['content'] == 'EXPECTED_REFERENCE_BODY')
            current = await call(client, 'read_file', workspace='project', path='main.py')
            changed = await call(client, 'run_task_step', task_id=task['task_id'], step_id='edit', operation='write_file', arguments={'path': 'main.py', 'content': 'value = 2\n', 'expected_sha256': current['sha256']}, action_id='write')
            await call(client, 'update_plan', task_id=task['task_id'], expected_revision=changed['task']['revision'], plan=[{'id':'edit','step':'Edit following local guidance','status':'completed'}])
            check('existing_project_edit_and_finish_compatible', (await call(client, 'finish_task', task_id=task['task_id']))['completed'])
            check('no_credentials_discovery', await rejected(client, 'get_project_context', workspace='project', path='.ssh/config'))
            first = await call(client, 'get_project_context', workspace='project', path='module', limit=1)
            second = await call(client, 'get_project_context', workspace='project', path='module', limit=1, offset=first['next_offset'])
            check('catalog_pagination_not_false_absence', first['skills'][0]['skill_path'] != second['skills'][0]['skill_path'] and first['total_skills'] == second['total_skills'])
            found = await call(client, 'find_skills', workspace='project', query='帮我读一下团队知识库文档，改一下知识库页面', path='module')
            check('chinese_trigger_phrase_outranks_character_overlap', found['matches'][0]['name'] == 'km-docs' and '团队知识库' in found['matches'][0]['matched'] and found['strong_matches'] == ['km-docs'] and all(m['name'] != 'city-notes' or m['score'] < found['matches'][0]['score'] / 3 for m in found['matches']))
            ranked = await call(client, 'get_project_context', workspace='project', path='module', query='团队知识库文档')
            check('context_query_ranks_trigger_hit_first', ranked['top_matches'][0]['name'] == 'km-docs' and ranked['skills'][0]['name'] == 'km-docs' and 'triggers' in ranked['skills'][0])
            named = await call(client, 'find_skills', workspace='project', query='用 money 这个 skill 处理金额', path='module')
            check('user_named_skill_is_top_match', named['matches'][0]['name'] == 'money' and 'money' in named['matches'][0]['matched'])
            check('no_match_reports_ordinary_tools', (await call(client, 'find_skills', workspace='project', query='烤面包机怎么修', path='module'))['strong_matches'] == [])
            status = await call(client, 'device_status')
            check('device_status_lists_skill_index_with_triggers', any(e['name'] == 'km-docs' and '团队知识库' in e['triggers'] for e in status['skill_index']['skills']) and status['skill_index']['total'] >= 5 and 'find_skills' in status['skill_routing'])
            check('workspace_mode_reports_global_rules_outside_scope', status['global_instructions'] == [] and any('AGENTS.md' in e['path'] for e in status['global_instruction_errors']))
            routed = await call(client, 'create_task', workspace='project', objective='Route a request through a local skill.',
                                plan=[{'id': 'route', 'step': 'Find and read the skill', 'status': 'in_progress'}],
                                checks=[{'kind': 'file_equals', 'path': 'main.py', 'value': 'value = 2\n'}])
            observed = await call(client, 'inspect_task_step', task_id=routed['task_id'], step_id='route', operation='find_skills', arguments={'query': '团队知识库', 'path': 'module'}, action_id='find')
            check('find_skills_receipt_recorded', found['receipt_id'] and observed['action']['result']['matches'][0]['name'] == 'km-docs' and observed['task']['action_count'] == 0)
        # Full-machine mode supports real symlinked/user/legacy skill libraries,
        # while still protecting the LocalPilot controller and package boundary.
        user = skill(base/'user-skills/own', 'user-own', 'User-wide fixture.')
        legacy = skill(work/'.claude/skills/legacy', 'legacy', 'Legacy project skill.')
        settings = json.loads(config.read_text())
        settings.update(permission_mode='full_machine', local_skill_roots=[str(user.parent.parent), str(home/'.claude/skills')], durable_jobs=False)
        config.write_text(json.dumps(settings))
        async with Client(args) as client:
            context = await call(client, 'get_project_context', workspace='project', path=str(work/'module'))
            entries = {s['skill_path']: s for s in context['skills']}
            check('full_machine_user_and_legacy_skills', entries[str(user)]['scope'] == 'user' and str(legacy) in entries)
            machine = await call(client, 'get_project_context', workspace='machine', path='/', query='读取团队知识库文档 docs.example.com')
            check('machine_root_context_reads_global_rules_and_ranks_citadel', any('docs-cli workspace' in d['content'] for d in machine['global_instructions']) and machine['top_matches'][0]['name'] == 'citadel')
            status = await call(client, 'device_status')
            check('device_status_global_excerpt_carries_rule', any('docs-cli workspace' in d['excerpt'] for d in status['global_instructions']) and any(e['name'] == 'citadel' for e in status['skill_index']['skills']))
            skill(home/'.claude/skills/late-arrival', 'late-arrival', '晚到的技能。触发词：晚到测试。')
            check('new_skill_visible_without_restart', (await call(client, 'find_skills', workspace='machine', query='晚到测试', path='/'))['matches'][0]['name'] == 'late-arrival')
            check('symlink_skill_deduplicated_no_loop', str(outside) in entries and len([s for s in context['skills'] if s['skill_path'] == str(primary)]) == 1)
            external = await call(client, 'read_local_skill', workspace='project', project_path=str(work), skill_path=str(outside))
            check('discovered_symlink_skill_readable', 'BODY_ONLY_ON_DEMAND' in external['content'])
            check('full_machine_reference_symlink_escape_rejected', await rejected(client, 'read_local_skill', workspace='project', project_path=str(work), skill_path=str(primary), resource_path='references/escape.md'))
            check('controller_paths_still_protected', await rejected(client, 'get_project_context', workspace='machine', path=str(source/'agent')))
        defaults = json.loads(config.read_text()); defaults.pop('local_skill_roots'); config.write_text(json.dumps(defaults))
        async with Client(args) as client:
            status = await call(client, 'device_status')
            check('default_roots_include_claude_skill_library', str(home/'.claude/skills') in status['local_skills']['user_directories'] and any(e['name'] == 'citadel' for e in status['skill_index']['skills']))
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'scope': 'Real local MCP; no model/API or browser claims',
              'checks': checks, 'passed': sum(c['passed'] for c in checks), 'total': len(checks)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'total': report['total'], 'report': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1]/'verification/local-skills/protocol.json')
    asyncio.run(verify(parser.parse_args().output))
