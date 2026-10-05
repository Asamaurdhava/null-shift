import asyncio, json, os
from aiohttp import ClientSession

BASE_URL=os.environ.get('NULLSHIFT_TEST_URL','http://127.0.0.1:8765').rstrip('/')
URL=f'{BASE_URL}/ws'

async def recv_until(ws, predicate, timeout=8):
    async def inner():
        while True:
            msg=await ws.receive()
            if msg.type.name!='TEXT':
                raise AssertionError(f'unexpected message {msg.type}')
            data=json.loads(msg.data)
            if predicate(data):
                return data
    return await asyncio.wait_for(inner(), timeout)

async def send(ws, payload):
    await ws.send_str(json.dumps(payload))


async def recv_close(ws, timeout=5):
    async def inner():
        while True:
            msg=await ws.receive()
            if msg.type.name in {'CLOSE','CLOSED','CLOSING'}:
                return msg
    return await asyncio.wait_for(inner(), timeout)

async def main():
    async with ClientSession() as session:
        a=await session.ws_connect(URL)
        b=await session.ws_connect(URL)
        await send(a, {'type':'create_room','name':'Alpha'})
        ident_a=(await recv_until(a,lambda m:m.get('type')=='identity'))
        code=ident_a['code']
        await recv_until(a,lambda m:m.get('type')=='state')

        await send(b, {'type':'join_room','code':code,'name':'Bravo'})
        ident_b=await recv_until(b,lambda m:m.get('type')=='identity')
        state_b=(await recv_until(b,lambda m:m.get('type')=='state'))['state']
        assert len(state_b['players'])==2
        state_a=(await recv_until(a,lambda m:m.get('type')=='state' and len(m['state']['players'])==2))['state']
        assert {p['name'] for p in state_a['players']}=={'Alpha','Bravo'}

        await send(a, {'type':'start'})
        await recv_until(a,lambda m:m.get('type')=='state' and m['state']['phase']=='COUNTDOWN')
        await recv_until(b,lambda m:m.get('type')=='state' and m['state']['phase']=='COUNTDOWN')
        await recv_until(a,lambda m:m.get('type')=='state' and m['state']['phase']=='MATCH',timeout=7)
        await recv_until(b,lambda m:m.get('type')=='state' and m['state']['phase']=='MATCH',timeout=7)

        # Protocol validation: host cannot reset a live game via a crafted frame.
        await send(a, {'type':'rematch'})
        active_rematch_error=await recv_until(a,lambda m:m.get('type')=='error')
        assert 'after results' in active_rematch_error['message']

        await send(a, {'type':'capture','nodeId':0})
        sa=(await recv_until(a,lambda m:m.get('type')=='state' and m['state']['nodes'][0]['owner']==ident_a['playerId']))['state']
        sb=(await recv_until(b,lambda m:m.get('type')=='state' and m['state']['nodes'][0]['owner']==ident_a['playerId']))['state']
        assert sa['nodes'][0]['owner']==sb['nodes'][0]['owner']==ident_a['playerId']

        await send(b, {'type':'capture','nodeId':0})
        await recv_until(a,lambda m:m.get('type')=='state' and m['state']['nodes'][0]['owner']==ident_b['playerId'])
        await recv_until(b,lambda m:m.get('type')=='state' and m['state']['nodes'][0]['owner']==ident_b['playerId'])

        # Power validation is server-authoritative and synchronized.
        await send(a, {'type':'capture','nodeId':1})
        await recv_until(a,lambda m:m.get('type')=='state' and m['state']['nodes'][1]['owner']==ident_a['playerId'])
        await recv_until(b,lambda m:m.get('type')=='state' and m['state']['nodes'][1]['owner']==ident_a['playerId'])
        await send(a, {'type':'power','power':'shield','nodeId':1})
        await recv_until(b,lambda m:m.get('type')=='state' and m['state']['nodes'][1]['shieldedBy']==ident_a['playerId'])
        await send(b, {'type':'capture','nodeId':1})
        blocked=await recv_until(b,lambda m:m.get('type')=='error')
        assert 'shielded' in blocked['message'].lower()
        await send(a, {'type':'power','power':'chaos','nodeId':1})
        shift_a=(await recv_until(a,lambda m:m.get('type')=='state' and m['state']['shift'] is not None))['state']['shift']['type']
        shift_b=(await recv_until(b,lambda m:m.get('type')=='state' and m['state']['shift'] is not None))['state']['shift']['type']
        assert shift_a==shift_b

        # Secret objective privacy: Alpha sees Alpha's secret, Bravo does not.
        await send(a, {'type':'ping'})
        # Grab next state snapshots (ticker broadcasts continuously).
        sa=(await recv_until(a,lambda m:m.get('type')=='state'))['state']
        sb=(await recv_until(b,lambda m:m.get('type')=='state'))['state']
        alpha_a=next(p for p in sa['players'] if p['id']==ident_a['playerId'])
        alpha_b=next(p for p in sb['players'] if p['id']==ident_a['playerId'])
        assert alpha_a['secret'] is not None and alpha_b['secret'] is None

        # Authenticated rejoin to same active room.
        await b.close()
        await asyncio.sleep(.2)
        b2=await session.ws_connect(URL)
        await send(b2, {'type':'rejoin','code':code,'playerId':ident_b['playerId'],'token':ident_b['token']})
        await recv_until(b2,lambda m:m.get('type')=='identity')
        rejoined=(await recv_until(b2,lambda m:m.get('type')=='state'))['state']
        assert rejoined['phase']=='MATCH'
        assert rejoined['viewerId']==ident_b['playerId']

        # A second live session replaces the first without marking the player
        # disconnected when the old socket's cleanup runs.
        b3=await session.ws_connect(URL)
        await send(b3, {'type':'rejoin','code':code,'playerId':ident_b['playerId'],'token':ident_b['token']})
        await recv_until(b3,lambda m:m.get('type')=='identity')
        replacement=(await recv_until(b3,lambda m:m.get('type')=='state'))['state']
        bravo=next(p for p in replacement['players'] if p['id']==ident_b['playerId'])
        assert bravo['connected'] is True
        closed=await recv_close(b2)
        assert closed.data==4001

        # Unauthorized rejoin fails.
        c=await session.ws_connect(URL)
        await send(c, {'type':'rejoin','code':code,'playerId':ident_a['playerId'],'token':'wrong'})
        err=await recv_until(c,lambda m:m.get('type')=='error')
        assert 'Invalid reconnect token' in err['message']

        # One physical socket cannot create/join multiple rooms and leave a
        # ghost player behind in the first room.
        d=await session.ws_connect(URL)
        await send(d, {'type':'create_room','name':'Delta'})
        await recv_until(d,lambda m:m.get('type')=='identity')
        await recv_until(d,lambda m:m.get('type')=='state')
        await send(d, {'type':'create_room','name':'Ghost'})
        rebound=await recv_until(d,lambda m:m.get('type')=='error')
        assert 'already joined' in rebound['message']

        await a.close(); await b3.close(); await c.close(); await d.close()
        print('WS INTEGRATION PASS', code)

if __name__=='__main__': asyncio.run(main())
