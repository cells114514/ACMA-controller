"""Cross-provider hit evidence; names alone do not identify a control."""
import math


def match_element(expected, check, pid, root):
    if check.get('hit_pid') != pid or check.get('hit_root') != root:
        return {'confirmed': False, 'reason': 'different_process_or_root'}
    frame = expected.get('frame') or {}
    if not all(type(frame.get(k)) in (int, float) and math.isfinite(frame[k]) for k in ('x', 'y', 'w', 'h')) or min(frame['w'], frame['h']) <= 0:
        return {'confirmed': False, 'reason': 'missing_expected_element_geometry'}
    matches = []
    chain=check.get('uia_chain', [])
    for index,node in enumerate(chain):
        if node.get('pid') != pid:
            continue
        role = node.get('role')
        # Classic Win32 Edit has a different RawView provider from Cua's UIA
        # provider: Pane + native class Edit represents the same edit surface.
        classic_edit = role == 'Pane' and node.get('class') == 'Edit' and node.get('native_hwnd') == check.get('hit_hwnd') and bool(node.get('native_hwnd'))
        classic_button=(role=='Pane' and expected.get('role')=='Button' and node.get('class')=='Button'
                        and bool(node.get('native_hwnd')) and node.get('native_hwnd')==check.get('hit_hwnd'))
        if role != expected.get('role') and not (expected.get('role') == 'Edit' and classic_edit) and not classic_button:
            continue
        rect = node.get('frame') or {}
        if not all(type(rect.get(k)) in (int, float) and math.isfinite(rect[k]) and abs(rect[k] - frame[k]) <= 2 for k in ('x', 'y', 'w', 'h')):
            continue
        if not (rect['x'] <= check['x'] < rect['x'] + rect['w'] and rect['y'] <= check['y'] < rect['y'] + rect['h']):
            continue
        name = node.get('name') or ''
        parent=chain[index+1] if index+1<len(chain) else {}
        # Native Edit's RawView Name may be its current text, while its
        # immediately enclosing labelled host provides Cua's accessible name.
        value_as_name=(classic_edit and isinstance(expected.get('value'),str)
                       and name==expected['value'] and bool(expected.get('label'))
                       and parent.get('pid')==pid and parent.get('name')==expected['label'])
        if name and name != (expected.get('label') or '') and not value_as_name:
            continue
        matches.append(node)
    if len(matches) != 1:
        return {'confirmed': False, 'reason': 'element_match_missing_or_ambiguous', 'matching_candidates': len(matches)}
    node = matches[0]
    return {'confirmed': True, 'reason': 'same_root_process_role_and_bounds',
            'name_matches': node.get('name') == expected.get('label'),
            'native_hwnd': node.get('native_hwnd'), 'runtime_id': node.get('runtime_id'),
            'matched_role': node.get('role'), 'matched_frame': node.get('frame'),
            'label_source': 'self' if node.get('name')==expected.get('label') else 'native_value_and_direct_parent' if node.get('name') else 'empty_name_geometry'}


def verify_probe(data, source, pid, root):
    expected = source.get('expected_element')
    # Geometry/window failures must never be relaxed by an element fallback.
    if not expected or data.get('reason') not in {'all_points_hit_exact_target_root', 'hit_test_identity_occlusion_or_label_mismatch'}:
        return data
    checks = data.get('checks') or []
    bounds=source.get('expected_bounds') or {}
    if all(type(bounds.get(k)) in (int,float) for k in ('x','y','width','height')) and any(
            not (bounds['x']<=c.get('x',float('inf'))<bounds['x']+bounds['width'] and bounds['y']<=c.get('y',float('inf'))<bounds['y']+bounds['height']) for c in checks):
        data.update(confirmed=False,reason='target_outside_observed_window_bounds',expected_element=expected)
        return data
    for check in checks:
        check['element_match'] = match_element(expected, check, pid, root)
        check['confirmed'] = check['element_match']['confirmed']
    data['confirmed'] = bool(checks) and all(c['confirmed'] for c in checks)
    data['reason'] = 'element_identity_evidence_matched' if data['confirmed'] else 'element_identity_evidence_unconfirmed'
    data['expected_element'] = expected
    return data


def coarse_container_only(data, pid, root):
    """Only the provider's missing child case is eligible; conflicts stay refused."""
    checks=data.get('checks') or []
    return (data.get('reason')=='element_identity_evidence_unconfirmed' and bool(checks)
            and all(c.get('hit_pid')==pid and c.get('hit_root')==root and c.get('uia_chain')
                    and all(n.get('pid')==pid and n.get('role') in {'Pane','Window'} for n in c['uia_chain'])
                    and c.get('element_match',{}).get('matching_candidates')==0 for c in checks))


def fresh_provider_match(source, state, pid, root):
    expected=source.get('expected_element') or {}
    if (state.get('pid'),state.get('window_id'))!=(pid,root) or state.get('error') or state.get('effect')=='refused':return None
    if state.get('elements_complete') is not True or state.get('truncated'):return None
    if state.get('window_bounds')!=source.get('expected_bounds'):return None
    frame=expected.get('frame') or {}
    if not expected.get('label') or not expected.get('role'):return None
    matches=[]
    for e in state.get('elements',[]):
        rect=e.get('frame') or {}
        if e.get('role')!=expected['role'] or e.get('label')!=expected['label']:continue
        if not all(type(rect.get(k)) in (int,float) and type(frame.get(k)) in (int,float)
                   and math.isfinite(rect[k]) and math.isfinite(frame[k]) and abs(rect[k]-frame[k])<=2 for k in ('x','y','w','h')):continue
        if min(rect['w'],rect['h'])<=0:continue
        matches.append(e)
    if len(matches)!=1 or not matches[0].get('enabled') or not matches[0].get('element_token'):return None
    element=matches[0]
    if not element.get('actions'):return None
    if any(not (element['frame']['x']<=point[0]<element['frame']['x']+element['frame']['w'] and
                element['frame']['y']<=point[1]<element['frame']['y']+element['frame']['h']) for point in source.get('points',[])):return None
    return element
