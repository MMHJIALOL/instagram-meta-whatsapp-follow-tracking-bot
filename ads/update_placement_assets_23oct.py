"""Update only the existing 25 October ad's placement creative, with snapshots."""
import copy
import json
import sys
from pathlib import Path

import admanager as a

ROOT = a.HERE.parent
OUT = a.HERE / 'placement-update-23oct'
AD_ID = '120255290411990581'


def save(name, data):
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def read(name):
    return json.loads((OUT / name).read_text(encoding='utf-8'))


def prepare():
    old = a.graph('GET', AD_ID, params={'fields':
        'id,name,status,effective_status,campaign{id,name,status,daily_budget,lifetime_budget},'
        'adset{id,name,status,daily_budget,lifetime_budget,start_time,end_time,targeting,destination_type,promoted_object},'
        'creative{id,name,object_story_spec,asset_feed_spec,url_tags,degrees_of_freedom_spec},tracking_specs'})
    assert old['status'] == 'ACTIVE' and old['campaign']['status'] == 'ACTIVE'
    assert old['adset']['destination_type'] == 'WHATSAPP'
    if not (OUT / 'active-ad-before.json').exists():
        save('active-ad-before.json', old)
    uploads = read('placement-uploads.json') if (OUT / 'placement-uploads.json').exists() else {}
    for label, filename, dimensions in [
        ('feed_4x5', 'Kalakriti-Instagram-Feed-4x5.png', (1440, 1800)),
        ('reels_9x16', 'Kalakriti-Instagram-Reels-9x16.png', (1440, 2560)),
        ('square_1x1', 'Kalakriti-Instagram-Square-1x1.png', (1440, 1440)),
    ]:
        if label not in uploads:
            job = {'name': filename}
            a.send_image(job, ROOT / filename)
            assert (job['width'], job['height']) == dimensions, (label, job)
            uploads[label] = {k: job[k] for k in ('hash', 'width', 'height', 'name')}
            save('placement-uploads.json', uploads)
        print(label, uploads[label]['width'], uploads[label]['height'], flush=True)
    cr = old['creative']
    story = copy.deepcopy(cr['object_story_spec'])
    link = story.pop('link_data')
    spec = {
        'name': old['name'] + ' | Feed 4x5 + Reels 9x16 | 23 Oct',
        'object_story_spec': story,
        'asset_feed_spec': {
            'ad_formats': ['SINGLE_IMAGE'],
            'optimization_type': 'PLACEMENT',
            'images': [{'hash': img['hash'], 'adlabels': [{'name': label}]} for label, img in uploads.items()],
            'bodies': [{'text': link['message']}],
            'titles': [{'text': link['name']}],
            'descriptions': [{'text': link.get('description', '')}],
            'link_urls': [{'website_url': link['link']}],
            'call_to_action_types': [link['call_to_action']['type']],
            'call_to_actions': [copy.deepcopy(link['call_to_action'])],
            'asset_customization_rules': [
                {'customization_spec': {'publisher_platforms': ['facebook', 'instagram'],
                    'facebook_positions': ['story', 'facebook_reels'],
                    'instagram_positions': ['story', 'reels', 'profile_reels']},
                 'image_label': {'name': 'reels_9x16'}, 'priority': 1},
                {'customization_spec': {'publisher_platforms': ['facebook', 'instagram'],
                    'facebook_positions': ['feed'], 'instagram_positions': ['stream', 'profile_feed']},
                 'image_label': {'name': 'feed_4x5'}, 'priority': 2},
                {'customization_spec': {}, 'image_label': {'name': 'square_1x1'},
                 'priority': 3, 'is_default': True},
            ],
        },
    }
    for field in ('url_tags', 'degrees_of_freedom_spec'):
        if cr.get(field):
            spec[field] = cr[field]
    save('creative-request.json', spec)
    result = a.graph('POST', f'{a.ACT}/adcreatives', retries=0,
                     data=a.form({**spec, 'execution_options': ['validate_only']}))
    save('validation.json', result)
    print('Creative validation:', json.dumps(result), flush=True)


def apply():
    spec = read('creative-request.json')
    if (OUT / 'created-creative.json').exists():
        created = read('created-creative.json')
    else:
        created = a.graph('POST', f'{a.ACT}/adcreatives', data=a.form(spec), retries=0)
        save('created-creative.json', created)
    cid = created['id']
    if (OUT / 'creative-verified.json').exists():
        verified = read('creative-verified.json')
    else:
        verified = a.graph('GET', cid, params={'fields': 'id,name,object_story_spec,asset_feed_spec,call_to_action_type'})
        save('creative-verified.json', verified)
    assert verified['asset_feed_spec']['optimization_type'] == 'PLACEMENT'
    assert len(verified['asset_feed_spec']['images']) == 3
    assert verified['asset_feed_spec']['call_to_action_types'] == ['WHATSAPP_MESSAGE']
    assert verified['asset_feed_spec']['call_to_actions'] == spec['asset_feed_spec']['call_to_actions']
    assert verified['asset_feed_spec']['link_urls'][0]['website_url'] == spec['asset_feed_spec']['link_urls'][0]['website_url']
    current = a.graph('GET', AD_ID, params={'fields': 'id,status,creative{id}'})
    before = read('active-ad-before.json')
    assert current['status'] == before['status']
    assert current['creative']['id'] in (before['creative']['id'], cid)
    if current['creative']['id'] != cid:
        if not (OUT / 'ad-update-validation.json').exists():
            validation = a.graph('POST', AD_ID, retries=0, data=a.form({
                'creative': {'creative_id': cid}, 'execution_options': ['validate_only']}))
            save('ad-update-validation.json', validation)
        result = a.graph('POST', AD_ID, retries=0, data=a.form({'creative': {'creative_id': cid}}))
        save('ad-update-result.json', result)
    after = a.graph('GET', AD_ID, params={'fields':
        'id,name,status,effective_status,issues_info,creative{id,asset_feed_spec},'
        'campaign{id,name,status,daily_budget,lifetime_budget},'
        'adset{id,name,status,daily_budget,lifetime_budget,start_time,end_time,targeting,destination_type,promoted_object}'})
    save('active-ad-after.json', after)
    assert after['creative']['id'] == cid
    assert after['campaign'] == before['campaign']
    assert after['adset'] == before['adset']
    assert after['status'] == before['status']
    print(json.dumps({'ad_id': AD_ID, 'creative_id': cid, 'status': after['status'],
                      'effective_status': after['effective_status'], 'issues': after.get('issues_info', []),
                      'budget_and_targeting_preserved': True}), flush=True)


if __name__ == '__main__':
    try:
        {'prepare': prepare, 'apply': apply}[sys.argv[1]]()
    except a.MetaError as exc:
        save('last-error.json', exc.info())
        print(json.dumps(exc.info(), ensure_ascii=True), flush=True)
        sys.exit(1)
