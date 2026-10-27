"""Create the 31 October campaign paused, with resumable receipts and read-back checks."""
import copy
import json
from pathlib import Path
import sys

import admanager as a

OUT = a.HERE / '31oct-free'
SOURCE = '120255227520700581'
NAME = '31st Oct Diwali Special FREE Resin Pooja Thali Workshop'


def save(name, data):
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def read(name):
    return json.loads((OUT / name).read_text(encoding='utf-8'))


def prepare():
    source = read(f'source-{SOURCE}.json')
    uploads = read('uploads.json') if (OUT / 'uploads.json').exists() else {}
    for label, filename, ratio in [('square_1x1', 'IMG_0910.PNG', 1),
                                   ('reels_9x16', 'IMG_0908.PNG', 9/16),
                                   ('feed_4x5', 'IMG_0909.PNG', 4/5)]:
        if label not in uploads:
            job = {'name': filename}
            a.send_image(job, Path.home() / 'Downloads' / filename)
            assert abs(job['width']/job['height'] - ratio) < .002, job
            uploads[label] = {k: job[k] for k in ('hash', 'width', 'height', 'name')}
            save('uploads.json', uploads)
        print(label, uploads[label]['width'], uploads[label]['height'], flush=True)

    old = source['ads']['data'][0]['creative']
    story = copy.deepcopy(old['object_story_spec'])
    link = story['link_data']
    link.update(image_hash=uploads['square_1x1']['hash'],
        message='🪔 Diwali Special FREE online Resin Art Pooja Thali Workshop\n'
                '📅 31 October (Saturday) | ⏰ 12–3 PM IST (3 ghante)\n'
                'Online, kahin se bhi join karo. Video recording bhi available hogi 🎥\n\n'
                'Workshop details aur WhatsApp group link ke liye neeche message karo.',
        name='FREE Resin Pooja Thali Workshop | 31 Oct, 12 PM')
    welcome = json.loads(link['page_welcome_message'])
    welcome['text_format']['message'].update(
        ice_breakers=[{'title': q} for q in ['Workshop kab hai?', 'Kya ye free hai?', 'Recording available hogi?']],
        text='Hi! 🪔 31 October ke FREE Diwali special resin workshop ke baare mein poochhein. Neeche se chuno.',
        quick_replies=[])
    welcome.pop('template_id', None)
    link['page_welcome_message'] = json.dumps(welcome, ensure_ascii=False)
    spec = {'name': NAME, 'object_story_spec': {k: v for k, v in story.items() if k != 'link_data'},
        'page_welcome_message': link['page_welcome_message'],
        'asset_feed_spec': {
            'ad_formats': ['SINGLE_IMAGE'], 'optimization_type': 'PLACEMENT',
            'images': [{'hash': im['hash'], 'adlabels': [{'name': label}]} for label, im in uploads.items()],
            'bodies': [{'text': link['message']}], 'titles': [{'text': link['name']}],
            'descriptions': [{'text': ''}], 'link_urls': [{'website_url': link['link']}],
            'call_to_action_types': [link['call_to_action']['type']],
            'call_to_actions': [copy.deepcopy(link['call_to_action'])],
            'asset_customization_rules': [
                {'customization_spec': {'publisher_platforms': ['instagram'],
                    'instagram_positions': ['story', 'reels']},
                 'image_label': {'name': 'reels_9x16'}, 'priority': 1},
                {'customization_spec': {'publisher_platforms': ['instagram'],
                    'instagram_positions': ['stream', 'profile_feed']},
                 'image_label': {'name': 'feed_4x5'}, 'priority': 2},
                {'customization_spec': {}, 'image_label': {'name': 'square_1x1'},
                 'priority': 3, 'is_default': True}]
        }, 'degrees_of_freedom_spec': copy.deepcopy(old['degrees_of_freedom_spec'])}
    save('creative-request.json', spec)
    result = a.graph('POST', f'{a.ACT}/adcreatives', retries=0,
                     data=a.form({**spec, 'execution_options': ['validate_only']}))
    save('creative-validation.json', result)
    print('Validated creative', json.dumps(result), flush=True)


def create_once(kind, edge, spec):
    filename = f'created-{kind}.json'
    if (OUT / filename).exists():
        return read(filename)['id']
    save(f'{kind}-request.json', spec)
    result = a.graph('POST', f'{a.ACT}/{edge}', retries=0, data=a.form(spec))
    save(filename, result)
    print('Created', kind, result['id'], flush=True)
    return result['id']


def create():
    source = read(f'source-{SOURCE}.json')
    cid = create_once('campaign', 'campaigns', {
        'name': NAME, 'objective': source['objective'], 'buying_type': source['buying_type'],
        'special_ad_categories': source['special_ad_categories'], 'status': 'PAUSED',
        'is_adset_budget_sharing_enabled': False})
    oldset = source['adsets']['data'][0]
    spec = {k: copy.deepcopy(oldset[k]) for k in ['daily_budget', 'targeting', 'optimization_goal',
                'billing_event', 'bid_strategy', 'destination_type']}
    spec.update(name=NAME + ' · audience', campaign_id=cid, status='PAUSED',
                promoted_object={'page_id': oldset['promoted_object']['page_id']},
                end_time='2026-10-31T12:00:00+05:30')
    sid = create_once('adset', 'adsets', spec)
    crid = create_once('creative', 'adcreatives', read('creative-request.json'))
    aid = create_once('ad', 'ads', {'name': NAME, 'adset_id': sid,
                     'creative': {'creative_id': crid}, 'status': 'PAUSED'})
    d = a.graph('GET', aid, params={'fields':
        'id,name,status,effective_status,created_time,issues_info,preview_shareable_link,'
        'campaign{id,name,status,objective},adset{id,name,status,daily_budget,targeting,end_time,destination_type},'
        'creative{id,object_story_spec,asset_feed_spec,page_welcome_message}'})
    save('verified.json', d)
    assert d['status'] == d['campaign']['status'] == d['adset']['status'] == 'PAUSED'
    assert d['adset']['daily_budget'] == oldset['daily_budget']
    assert d['adset']['targeting'] == oldset['targeting']
    feed = d['creative']['asset_feed_spec']
    assert len(feed['images']) == 3 and feed['optimization_type'] == 'PLACEMENT'
    assert feed['call_to_action_types'] == ['INSTAGRAM_MESSAGE']
    saved = json.loads(d['creative'].get('page_welcome_message') or
                       d['creative']['object_story_spec']['link_data']['page_welcome_message'])
    qs = saved['text_format']['message']['ice_breakers']
    assert [q['title'] for q in qs] == ['Workshop kab hai?', 'Kya ye free hai?', 'Recording available hogi?']
    print(json.dumps({'campaign_id': cid, 'adset_id': sid, 'ad_id': aid, 'created_time': d['created_time'],
                      'status': d['status'], 'questions': qs, 'issues': d.get('issues_info')}), flush=True)


if __name__ == '__main__':
    try:
        {'prepare': prepare, 'create': create}[sys.argv[1]]()
    except a.MetaError as exc:
        save('last-error.json', exc.info())
        print(json.dumps(exc.info()), flush=True)
        raise SystemExit(1)
