"""Read-only evidence for a new owned window after a dispatched click."""
import re


def post_click_window(error):
    match=re.search(r'foreground_unavailable:.*after the click \(actual foreground HWND (0x[0-9a-fA-F]+)\)',str(error))
    return int(match[1],16) if match else None


def verify_transition(target,before,after,probe,candidate_hwnd,source_identity=None,candidate_state=None):
    result={'status':'unverified','input_replayed':False,'windows':after}
    original=next((w for w in before if (w.get('pid'),w.get('window_id'))==(target['pid'],target['window_id'])),None)
    candidates=[w for w in after if w.get('window_id')==candidate_hwnd and w.get('is_on_screen')]
    if not original or len(candidates)!=1:
        return {**result,'reason':'not_a_unique_new_visible_window'}
    candidate=candidates[0];source=probe.get('target',{});destination=probe.get('candidate',{})
    if (destination.get('pid'),destination.get('window_id'))!=(candidate.get('pid'),candidate_hwnd):
        return {**result,'reason':'native_identity_mismatch','native_evidence':probe}
    prior=source_identity or {}
    source_disappeared=not any((w.get('pid'),w.get('window_id'))==(target['pid'],target['window_id']) for w in after)
    exact_prior=(prior.get('pid'),prior.get('window_id'))==(target['pid'],target['window_id'])
    same_prior_program=bool(prior.get('exe_path') and destination.get('exe_path') and prior['exe_path'].casefold()==destination['exe_path'].casefold())
    same_prior_session=prior.get('session_id') is not None and prior.get('session_id')==destination.get('session_id')
    readable=bool(candidate_state and not candidate_state.get('error') and candidate_state.get('effect')!='refused'
                  and (candidate_state.get('pid'),candidate_state.get('window_id'))==(candidate.get('pid'),candidate_hwnd)
                  and candidate_state.get('elements'))
    foreground_confirmed=probe.get('foreground_hwnd')==candidate_hwnd
    if (source_disappeared and exact_prior and prior.get('process_started_at') and prior.get('checked_at_utc') and same_prior_program and same_prior_session
            and destination.get('visible') and (foreground_confirmed or readable) and candidate_hwnd!=target['window_id']):
        return {**result,'status':'verified','reason':'source_disappeared_same_executable_session_verified_handoff',
                'source':target,'next_target':{k:candidate[k] for k in ('pid','window_id','app_name','title') if k in candidate},
                'source_identity_before_click':prior,'native_evidence':probe,
                'foreground':{'confirmed':foreground_confirmed,'observed_hwnd':probe.get('foreground_hwnd'),'activation_required_before_input':not foreground_confirmed},
                'candidate_readback':({'pid':candidate_state['pid'],'window_id':candidate_hwnd,'snapshot_id':candidate_state.get('snapshot_id'),
                    'element_count':len(candidate_state['elements'])} if readable else None)}
    if any(w.get('window_id')==candidate_hwnd for w in before):return {**result,'reason':'not_a_unique_new_visible_window'}
    if (source.get('pid'),source.get('window_id'))!=(target['pid'],target['window_id']) or (destination.get('pid'),destination.get('window_id'))!=(candidate.get('pid'),candidate_hwnd):
        return {**result,'reason':'native_identity_mismatch','native_evidence':probe}
    source_path=source.get('exe_path');new_path=destination.get('exe_path')
    same_program=bool(source_path and new_path and source_path.casefold()==new_path.casefold())
    same_session=source.get('session_id') is not None and source.get('session_id')==destination.get('session_id')
    owned=target['window_id'] in destination.get('owner_chain',[])
    if not (same_program and same_session and owned and destination.get('visible')):
        return {**result,'reason':'owner_session_or_program_unconfirmed','native_evidence':probe}
    return {**result,'status':'verified','reason':'new_visible_window_same_executable_session_and_exact_owner_chain',
            'source':target,'next_target':{k:candidate[k] for k in ('pid','window_id','app_name','title') if k in candidate},
            'native_evidence':probe}
