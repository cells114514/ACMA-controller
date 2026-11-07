"""Bounded deterministic retrieval; acquisition and output budgets are separate."""
import json

COLLECTION = {'max_elements': 20000, 'max_depth': 80, 'timeout_ms': 10000}


def search_snapshot(state, args):
    elements = state.get('elements', [])
    indexed = {e.get('element_index'): e for e in elements if type(e.get('element_index')) is int}
    fields = args.get('fields', ['label', 'value', 'automation_id'])
    query = args['query'].casefold()
    scope = args.get('scope', 'window')
    matches = []
    total = 0
    clipped = False
    for element in elements:
        if args.get('role') and element.get('role') != args['role']:continue
        web = element.get('in_web_content')
        if scope == 'web' and web is not True:continue
        if scope == 'browser' and web is True:continue
        matched = [field for field in fields if isinstance(element.get(field), str)
                   and (element[field].casefold() == query if args.get('match') == 'exact'
                        else query in element[field].casefold())]
        if not matched:continue
        total += 1
        if len(matches) >= args.get('max_results', 5):continue
        item = {}
        for key in ('role','label','value','automation_id','enabled','element_index',
                    'element_token','frame','screenshot_frame','in_web_content','actions'):
            if key in element:
                value = element[key]
                if isinstance(value, str) and len(value) > 512:
                    value = value[:512];clipped = True
                    item.setdefault('clipped_fields', []).append(key)
                item[key] = value
        item.update(matched_fields=matched,context=[])
        parent = element.get('parent_index');seen = set()
        while parent in indexed and parent not in seen and len(item['context']) < 2:
            seen.add(parent);p = indexed[parent]
            item['context'].append({k: str(p[k])[:160] for k in ('role','label') if k in p})
            parent = p.get('parent_index')
        f = element.get('frame', {});b = state.get('window_bounds', {})
        if all(k in f for k in ('x','y','w','h')) and all(k in b for k in ('x','y','width','height')):
            item['viewport_intersection'] = (f['w'] > 0 and f['h'] > 0
                and f['x'] < b['x'] + b['width'] and f['x'] + f['w'] > b['x']
                and f['y'] < b['y'] + b['height'] and f['y'] + f['h'] > b['y'])
        else:item['viewport_intersection'] = 'unknown'
        matches.append(item)
    complete = state.get('elements_complete') is True and state.get('truncated') is False
    result = {'status': 'matched' if total else 'not_found' if complete else 'incomplete',
              'source': 'windows_uia', 'pid': args['pid'], 'window_id': args['window_id'],
              'snapshot_id': state.get('snapshot_id'), 'query': args['query'], 'scope': scope,
              'matches': matches, 'matched_count': total, 'search_complete': complete,
              'matched_count_is_lower_bound': not complete,
              'collection_truncated': not complete, 'truncation_reason': state.get('truncation_reason'),
              'nodes_visited': state.get('nodes_visited'), 'nodes_pending': state.get('nodes_pending'),
              'collected_element_count': len(elements), 'returned_count': len(matches),
              'output_truncated': len(matches) < total, 'text_clipped': clipped,
              'scope_note': 'browser excludes annotated web content; unannotated origin remains unverified',
              'reference_note': 'Snapshot-bound references; refresh after actions or another read. Viewport intersection does not prove visibility, focus or absence of occlusion.'}
    while matches and len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > 12000:
        matches.pop();result.update(returned_count=len(matches),output_truncated=True)
    return result
