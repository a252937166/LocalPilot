"""Image bytes, permissions, bounded payloads and harness replay through real MCP."""
from __future__ import annotations
import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import zlib
from datetime import datetime, timezone
from PIL import Image, PngImagePlugin
from mcp import Client, StdioServerParameters


def sha(data):
    return hashlib.sha256(data).hexdigest()


async def verify(output):
    source = Path(__file__).resolve().parents[1]
    results = []
    def check(name, passed):
        results.append({'name': name, 'passed': bool(passed)})
        assert passed, name
    async def call(client, name, **args):
        result = await client.call_tool(name, args)
        assert not result.is_error, (name, [getattr(c, 'text', '') for c in result.content])
        return result
    def pixels(result):
        blocks = [c for c in result.content if c.type == 'image']
        assert len(blocks) == 1
        raw = base64.b64decode(blocks[0].data, validate=True)
        im = Image.open(io.BytesIO(raw)); im.load()
        return raw, im
    async def read(client, path='sample.png', **args):
        return await call(client, 'read_image', workspace='project', path=path, **args)
    with tempfile.TemporaryDirectory(prefix='localpilot-images-') as directory:
        root = Path(directory).resolve(); work = root/'work'; work.mkdir()
        state = root/'state'; config = root/'config/config.json'; config.parent.mkdir()
        config.write_text(json.dumps({'workspaces': {'project': str(work)}, 'state_dir': str(state), 'max_file_bytes': 16*1024*1024}))
        args = StdioServerParameters(command=sys.executable, args=['-I', str(source/'agent/server.py')], env={'LOCALPILOT_CONFIG': str(config)})
        fixture = Image.new('RGB', (200,100), 'red'); fixture.paste('blue',(100,0,200,100))
        info = PngImagePlugin.PngInfo(); info.add_text('Comment', 'PRIVATE_SOURCE_METADATA')
        fixture.save(work/'sample.png', pnginfo=info)
        original = (work/'sample.png').read_bytes()
        (work/'plain.txt').write_text('not a picture')
        (work/'broken.png').write_bytes(original[:70])
        fixture.save(root/'outside.png')
        (work/'link.png').symlink_to(root/'outside.png')
        (work/'huge.bin').write_bytes(b'x'*(16*1024*1024+1))
        # A valid PNG header with an impossible canvas; rejected before pixel allocation.
        header = struct.pack('>IIBBBBB',100000,100000,8,2,0,0,0)
        bomb = original[:8] + struct.pack('>I',13) + b'IHDR' + header + struct.pack('>I',zlib.crc32(b'IHDR'+header)) + original[33:]
        (work/'bomb.png').write_bytes(bomb)
        exif = Image.Exif(); exif[274] = 6; exif[270] = 'PRIVATE_EXIF'
        Image.new('RGB',(40,70),'green').save(work/'rotated.jpg',exif=exif)
        red = Image.new('RGB',(30,20),'red'); blue = Image.new('RGB',(30,20),'blue')
        red.save(work/'animated.gif',save_all=True,append_images=[blue],duration=100,loop=0)
        async with Client(args) as client:
            tools = {t.name:t for t in (await client.list_tools()).tools}
            check('read_image_is_readonly_and_exposed', tools['read_image'].annotations.read_only_hint is True)
            image_result = await read(client)
            raw, im = pixels(image_result); meta = image_result.structured_content
            check('native_image_block_not_base64_text', im.size == (200,100) and len(raw) == meta['image_bytes'] and
                  all(base64.b64encode(raw).decode() not in getattr(c,'text','') for c in image_result.content) and 'data' not in meta)
            check('source_and_delivery_hashes_match_actual_bytes', meta['sha256'] == sha(original) and meta['image_sha256'] == sha(raw))
            check('source_metadata_stripped', b'PRIVATE_SOURCE_METADATA' not in raw and not im.info)
            cropped = await read(client,crop=[100,0,200,100]); _, cropped_im = pixels(cropped)
            check('crop_uses_original_coordinates', cropped_im.size == (100,100) and cropped_im.getpixel((50,50)) == (0,0,255))
            rotated = await read(client,'rotated.jpg'); rotated_raw, rotated_im = pixels(rotated)
            check('exif_orientation_corrected_and_stripped', rotated_im.size == (70,40) and not rotated_im.getexif() and b'PRIVATE_EXIF' not in rotated_raw)
            _, frame = pixels(await read(client,'animated.gif',frame_index=1))
            check('animated_frame_selected', frame.getpixel((10,10)) == (0,0,255))
            for fmt, ext in [('JPEG','jpg'),('WEBP','webp'),('BMP','bmp'),('TIFF','tiff')]:
                fixture.save(work/f'format.{ext}',format=fmt)
                check('format_'+ext, (await read(client,f'format.{ext}')).structured_content['source_format'] == fmt)
            for name, path, kw in [('text','plain.txt',{}),('corrupt','broken.png',{}),('missing','missing.png',{}),
                                   ('oversize','huge.bin',{}),('pixel_bomb','bomb.png',{}),('outside',str(root/'outside.png'),{}),
                                   ('symlink','link.png',{}),('bad_crop','sample.png',{'crop':[0,0,999,50]}),
                                   ('empty_crop','sample.png',{'crop':[0,0,0,0]}),('bad_max_side','sample.png',{'max_side':8000}),
                                   ('bad_frame','animated.gif',{'frame_index':2})]:
                check('reject_'+name, (await client.call_tool('read_image',{'workspace':'project','path':path,**kw})).is_error)
            noisy = Image.frombytes('RGB',(2048,2048),os.urandom(2048*2048*3)); noisy.save(work/'noise.png')
            large = await read(client,'noise.png',max_side=2048); large_raw, large_im = pixels(large)
            check('large_image_delivery_bounded', len(large_raw) <= 1024*1024 and max(large_im.size) <= 2048)
            check('source_file_unchanged', (work/'sample.png').read_bytes() == original)
            excess = state/'image-snapshots'/('0'*64)
            excess.write_bytes(b'x'*(33*1024*1024)); os.utime(excess,(1,1))
            await read(client)
            check('snapshot_cache_is_bounded', not excess.exists() and sum(p.stat().st_size for p in excess.parent.iterdir()) <= 32*1024*1024)
            task = (await call(client,'create_task',workspace='project',objective='Inspect the local image without changing it.',
                                plan=[{'id':'see','step':'Inspect picture','status':'in_progress'}],
                                checks=[{'kind':'file_sha256','path':'sample.png','value':sha(original)}])).structured_content
            task_id = task['task_id']; action_args = dict(task_id=task_id,step_id='see',operation='read_image',arguments={'path':'sample.png'},action_id='view')
            observed = await call(client,'inspect_task_step',**action_args); task_pixels, _ = pixels(observed)
            task = observed.structured_content['task']
            check('harness_observation_has_image_without_mutation_budget', task['action_count'] == 0 and task['observation_count'] == 1 and task['last_mutation_at'] == 0)
            check('binary_sha256_acceptance_supported', task['checks_status']['passed'] == 1)
            Image.new('RGB',(200,100),'green').save(work/'sample.png')
            replay = await call(client,'inspect_task_step',**action_args)
            replay_bytes, _ = pixels(replay)
            check('replay_retains_original_pixels_after_source_change', replay.structured_content['replayed'] and replay_bytes == task_pixels)
            check('changed_source_fails_immutable_acceptance', replay.structured_content['task']['checks_status']['passed'] == 0)
            detail = (await call(client,'get_task_activity',task_id=task_id,action_id='view')).structured_content
            check('activity_contains_image_metadata_without_payload', detail['records'][0]['result']['width'] == 200 and 'data' not in detail['records'][0]['result'])
            with sqlite3.connect(state/'localpilot.sqlite3') as db:
                rows = [r[0] for r in db.execute('select snapshot from task_actions')] + [r[0] for r in db.execute('select details from events')]
            check('image_base64_never_persisted_in_receipts', all(base64.b64encode(task_pixels).decode() not in s for s in rows))
            await call(client,'set_task_state',task_id=task_id,status='paused',reason='Verify paused reads are blocked.')
            paused = await client.call_tool('inspect_task_step',{**action_args,'action_id':'paused'})
            check('paused_task_cannot_read_new_image', paused.is_error)
            await call(client,'set_task_state',task_id=task_id,status='active',reason='Continue verification.')
        async with Client(args) as client:
            replay = await call(client,'inspect_task_step',**action_args)
            check('image_snapshot_replay_survives_restart', pixels(replay)[0] == task_pixels)
            fresh = await call(client,'inspect_task_step',**{**action_args,'action_id':'fresh'})
            check('new_action_reads_current_pixels', pixels(fresh)[0] != task_pixels)
            key = replay.structured_content['action']['result']['image_sha256']
            cached = state/'image-snapshots'/key
            check('image_cache_private', cached.stat().st_mode & 0o777 == 0o600 and cached.parent.stat().st_mode & 0o777 == 0o700)
            cached.write_bytes(b'corrupt')
            check('snapshot_corruption_is_explicit_error', (await client.call_tool('inspect_task_step',action_args)).is_error)
            cached.unlink()
            check('evicted_snapshot_does_not_silently_reread_source', (await client.call_tool('inspect_task_step',action_args)).is_error)
        config.write_text(json.dumps({'permission_mode':'full_machine','workspaces':{'project':str(work)},'state_dir':str(state)}))
        async with Client(args) as client:
            check('full_machine_can_read_normal_absolute_image', pixels(await read(client,str(root/'outside.png')))[1].size == (200,100))
            check('full_machine_normal_symlink_works', pixels(await read(client,'link.png'))[1].size == (200,100))
            check('full_machine_control_state_stays_protected', (await client.call_tool('read_image',{'workspace':'machine','path':str(state/'localpilot.sqlite3')})).is_error)
    report = {'checked_at':datetime.now(timezone.utc).isoformat(),'scope':'Real local MCP image protocol and harness; not ChatGPT visual inference',
              'passed':sum(x['passed'] for x in results),'total':len(results),'checks':results}
    output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':report['passed'],'total':report['total'],'output':str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output',type=Path,default=Path('verification/images-v1/images.json'))
    asyncio.run(verify(parser.parse_args().output))
