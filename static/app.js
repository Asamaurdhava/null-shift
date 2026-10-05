(() => {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => [...document.querySelectorAll(s)];
  const colorMap = {cyan:'#50efff',magenta:'#ff4ff3',lime:'#a6ff4d',gold:'#ffd95a',orange:'#ff8b38',violet:'#9d6cff',ice:'#9edcff',crimson:'#ff4664'};
  const powerCosts = {shield:18,jam:22,overload:25,chaos:30};
  let ws, state = null, selectedNode = null, serverOffset = 0, joinMode = 'create', toastTimer;
  let retry = 0, intentionalClose = false;

  const screens = ['landing','lobby','game','results'];
  function showScreen(id){ screens.forEach(x => $('#'+x).classList.toggle('active', x===id)); }
  function toast(msg){ const el=$('#toast'); el.textContent=msg; el.classList.remove('hidden'); clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.classList.add('hidden'),2600); }
  function send(obj){ if(ws?.readyState===WebSocket.OPEN) ws.send(JSON.stringify(obj)); else toast('Connection unavailable'); }
  function now(){ return Date.now()+serverOffset; }
  function me(){ return state?.players.find(p=>p.id===state.viewerId); }
  function player(pid){ return state?.players.find(p=>p.id===pid); }
  function pcolor(pid){ const p=player(pid); return colorMap[p?.color]||'#44515b'; }
  function fmt(ms){ const sec=Math.max(0,Math.ceil(ms/1000)); return `${String(Math.floor(sec/60)).padStart(2,'0')}:${String(sec%60).padStart(2,'0')}`; }
  function activeShift(){ return state?.shift && state.shift.until > now() ? state.shift : null; }

  function setConnection(mode,text){ const c=$('#connection'); c.className='connection '+mode; c.lastElementChild.textContent=text; }
  function connect(rejoin=true){
    const proto=location.protocol==='https:'?'wss':'ws';
    ws=new WebSocket(`${proto}://${location.host}/ws`);
    setConnection('','CONNECTING');
    ws.onopen=()=>{ retry=0; setConnection('online','LIVE'); if(rejoin){ const saved=loadIdentity(); if(saved) send({type:'rejoin',...saved}); } };
    ws.onmessage=(ev)=>{
      let msg;
      try { msg=JSON.parse(ev.data); } catch { return; }
      if(msg.type==='identity'){
        localStorage.setItem('nullshift.identity',JSON.stringify({code:msg.code,playerId:msg.playerId,token:msg.token}));
      } else if(msg.type==='state'){
        state=msg.state; serverOffset=state.serverNow-Date.now(); render();
      } else if(msg.type==='error'){
        if(msg.message==='Room expired'||msg.message==='Invalid reconnect token'){
          localStorage.removeItem('nullshift.identity'); state=null; selectedNode=null; showScreen('landing');
        }
        toast(msg.message);
      }
    };
    ws.onclose=(event)=>{
      if(event.code===4001){ setConnection('offline','SESSION MOVED'); toast('This operator session moved to another tab or device.'); return; }
      setConnection('offline','RECONNECTING');
      if(!intentionalClose){ const delay=Math.min(4000,500*Math.pow(1.5,retry++)); setTimeout(()=>connect(true),delay); }
    };
    ws.onerror=()=>setConnection('offline','SIGNAL LOST');
  }
  function loadIdentity(){ try{return JSON.parse(localStorage.getItem('nullshift.identity')||'null')}catch{return null} }

  function render(){
    if(!state) return;
    if(state.serverNow) serverOffset=state.serverNow-Date.now();
    if(state.phase==='LOBBY') renderLobby();
    if(state.phase==='COUNTDOWN'||state.phase==='MATCH'){ renderGame(); renderCountdown(); }
    if(state.phase==='RESULTS') renderResults();
  }

  function renderLobby(){
    selectedNode=null;
    showScreen('lobby'); $('#countdown').classList.add('hidden');
    const connectedCount=state.players.filter(p=>p.connected).length;
    $('#copyCode').textContent=state.code; $('#playerCount').textContent=`${connectedCount}/8`;
    $('#lobbyPlayers').innerHTML=state.players.map(p=>`<div class="lobby-player" style="--pc:${colorMap[p.color]}"><span class="avatar"></span><strong>${esc(p.name)}</strong>${p.isHost?'<span class="host-tag">HOST</span>':''}<span>${p.connected?'●':'○'}</span></div>`).join('');
    const mine=me(), host=mine?.isHost;
    $('#startBtn').disabled=!host||connectedCount<2;
    $('#startBtn').classList.toggle('hidden',!host);
    $('#hostHint').textContent=host?(connectedCount<2?'Waiting for one more operator…':'You control launch.'): 'Waiting for the host to initiate NULL.';
  }

  function renderCountdown(){
    if(state.phase!=='COUNTDOWN'){ $('#countdown').classList.add('hidden'); return; }
    const left=Math.ceil((state.countdownEndsAt-now())/1000);
    $('#countdownNumber').textContent=left>0?left:'GO'; $('#countdown').classList.remove('hidden');
  }

  function renderGame(){
    showScreen('game');
    $('#gameRoom').textContent=state.code;
    const mine=me();
    $('#energy').textContent=Math.floor(mine?.energy||0);
    $('#secretText').textContent=mine?.secret?.text||'Mission encrypted';
    $('#secretBonus').textContent=mine?.secret?.bonus?`+${mine.secret.bonus} POINTS`:' '; 
    const ownedCounts=new Map(state.players.map(p=>[p.id,0]));
    state.nodes.forEach(n=>{ if(n.owner&&!n.collapsed) ownedCounts.set(n.owner,(ownedCounts.get(n.owner)||0)+1); });
    const scoreSorted=[...state.players].sort((a,b)=>(ownedCounts.get(b.id)||0)-(ownedCounts.get(a.id)||0)||b.score-a.score);
    $('#scoreboard').innerHTML=scoreSorted.map(p=>{const own=ownedCounts.get(p.id)||0;return `<div class="score-row ${p.id===state.viewerId?'me':''} ${!p.connected?'offline':''}" style="--pc:${colorMap[p.color]}"><span class="score-dot"></span><div class="score-meta"><strong>${esc(p.name)}</strong><small>${own} NODES · ${Math.floor(p.energy)} EN</small></div><span class="score-number">${p.score}</span></div>`}).join('');
    renderArena(); renderPowers(); renderFeed(); renderShift();
    const jammed=(mine?.jammedUntil||0)>now(); $('#jamOverlay').classList.toggle('hidden',!jammed);
  }

  function renderArena(){
    const blackout=activeShift()?.type==='BLACKOUT';
    const html=state.nodes.map(n=>{
      const own=n.owner, pc=pcolor(own); const cls=['node']; if(own)cls.push('owned'); if(n.reactor)cls.push('reactor'); if(n.collapsed)cls.push('collapsed'); if(n.shieldedBy)cls.push('shielded'); if(selectedNode===n.id)cls.push('selected'); if(blackout)cls.push('blackout');
      return `<button class="${cls.join(' ')}" data-node="${n.id}" style="--pc:${pc}" aria-label="Node ${n.id+1}${n.reactor?' Reactor':''}"><span class="nnum">${String(n.id+1).padStart(2,'0')}</span><span class="glyph">${n.reactor?'◇':own?'◆':'·'}</span></button>`;
    }).join('');
    $('#arena').innerHTML=html;
    $$('.node').forEach(btn=>btn.onclick=()=>nodeClick(Number(btn.dataset.node)));
    const n=selectedNode!=null?state.nodes[selectedNode]:null;
    if(!n) $('#selectedInfo').textContent='SELECT A NODE';
    else if(n.collapsed) $('#selectedInfo').textContent=`NODE ${String(n.id+1).padStart(2,'0')} · COLLAPSED`;
    else if(n.owner) $('#selectedInfo').textContent=`NODE ${String(n.id+1).padStart(2,'0')} · ${blackout?'OWNER MASKED':player(n.owner)?.name||'UNKNOWN'}`;
    else $('#selectedInfo').textContent=`NODE ${String(n.id+1).padStart(2,'0')} · NEUTRAL`;
  }
  function nodeClick(id){
    selectedNode=id; renderArena();
    const n=state.nodes[id]; if(state.phase!=='MATCH'||n.collapsed)return;
    if(n.owner!==state.viewerId) send({type:'capture',nodeId:id});
  }

  function renderPowers(){
    const mine=me(); const t=now();
    $$('.power').forEach(btn=>{
      const power=btn.dataset.power, cd=(mine?.cooldowns?.[power]||0)-t;
      const disabled=state.phase!=='MATCH'||(mine?.energy||0)<powerCosts[power]||cd>0||(mine?.jammedUntil||0)>t;
      btn.disabled=disabled; btn.classList.toggle('cooling',cd>0); btn.dataset.cd=cd>0?`${Math.ceil(cd/1000)}s`:'';
    });
  }
  function renderFeed(){ $('#feed').innerHTML=[...state.feed].reverse().slice(0,7).map(f=>`<div class="feed-item ${f.kind}">${esc(f.text)}</div>`).join(''); }
  function renderShift(){
    const banner=$('#shiftBanner');
    if(state.finalCollapse){ banner.classList.remove('hidden'); banner.classList.add('danger'); $('#shiftName').textContent='SYSTEM COLLAPSE'; $('#shiftTime').textContent='GRID DEGRADING'; return; }
    banner.classList.remove('danger');
    const shift=activeShift();
    if(shift){ banner.classList.remove('hidden'); $('#shiftName').textContent=shift.type; $('#shiftTime').textContent=`${Math.max(0,Math.ceil((shift.until-now())/1000))}s`; }
    else banner.classList.add('hidden');
  }

  function renderResults(){
    showScreen('results'); $('#countdown').classList.add('hidden'); $('#shiftBanner').classList.add('hidden'); $('#jamOverlay').classList.add('hidden');
    const winner=state.results[0]; $('#winnerName').textContent=winner?.name||'NO SURVIVOR'; $('#winnerName').style.color=winner?colorMap[winner.color]:'#fff';
    $('#resultList').innerHTML=state.results.map((r,i)=>`<div class="result-row"><span class="place">#${i+1}</span><div class="result-main"><strong style="color:${colorMap[r.color]}">${esc(r.name)}</strong><small>${r.owned} nodes · ${r.energy} energy · secret ${r.secretCompleted?'COMPLETE +'+r.secretBonus:'failed'}</small></div><span class="result-score">${r.score}</span></div>`).join('');
    const host=me()?.isHost; $('#rematchBtn').classList.toggle('hidden',!host); $('#rematchHint').textContent=host?'Host controls the rematch.':'Waiting for host to run it back.';
  }

  function esc(s){ return String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }

  $$('.tab').forEach(tab=>tab.onclick=()=>{ joinMode=tab.dataset.tab; $$('.tab').forEach(x=>x.classList.toggle('active',x===tab)); $('#joinCodeWrap').classList.toggle('hidden',joinMode==='create'); $('#primaryBtn').textContent=joinMode==='create'?'CREATE ROOM':'JOIN ROOM'; });
  $('#primaryBtn').onclick=()=>{ const name=$('#nameInput').value.trim(); if(!name)return toast('Enter an operator name'); if(joinMode==='create') send({type:'create_room',name}); else { const code=$('#codeInput').value.trim().toUpperCase(); if(code.length!==4)return toast('Enter the 4-character room code'); send({type:'join_room',code,name}); } };
  $('#codeInput').oninput=e=>e.target.value=e.target.value.toUpperCase().replace(/[^A-Z2-9]/g,'').slice(0,4);
  $('#copyCode').onclick=async()=>{ try{await navigator.clipboard.writeText(state.code);toast('Room code copied')}catch{toast(`Room code: ${state.code}`)} };
  $('#startBtn').onclick=()=>send({type:'start'});
  $('#rematchBtn').onclick=()=>send({type:'rematch'});
  $$('.power').forEach(btn=>btn.onclick=()=>{const power=btn.dataset.power; if((power==='shield'||power==='overload')&&selectedNode==null)return toast('Select a node first'); send({type:'power',power,nodeId:selectedNode});});

  setInterval(()=>{
    if(!state)return;
    if(state.phase==='COUNTDOWN') renderCountdown();
    if(state.phase==='MATCH'){
      const left=state.endsAt-now(), shift=activeShift(); $('#timer').textContent=fmt(left); $('#phaseLabel').textContent=state.finalCollapse?'SYSTEM COLLAPSE':shift?`SHIFT · ${shift.type}`:'SYSTEM STABLE'; renderPowers(); renderShift(); const mine=me(); $('#jamOverlay').classList.toggle('hidden',!((mine?.jammedUntil||0)>now())); if(left<=0) $('#timer').textContent='00:00';
    }
  },200);

  window.addEventListener('beforeunload',()=>intentionalClose=true);
  const saved=loadIdentity(); if(saved){ showScreen('landing'); }
  connect(true);
})();
