/* LOOM adapter: fixed data slots -> official AntV G2 v5 Spec Mode.
 * G2 owns drawing, tooltip placement, hit testing and legend filtering.
 * No network calls, generated JavaScript, or data-dependent HTML. */
(function (root) {
  'use strict';
  const chartKinds = ['comparison','trend','composition','distribution','relationship','matrix','waterfall','candlestick','gantt'];
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const number = value => finite(value) ? String(value) : '未提供';
  // Colours come from the page theme (loom-theme.css) when a document exists; static/Node rendering uses the light fallbacks.
  const cssVar = (name, fallback) => { try { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; } catch (e) { return fallback; } };
  const isDark = () => { try { const t = document.documentElement.getAttribute('data-theme'); return t ? t === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches; } catch (e) { return false; } };
  const palette = () => ({s1: cssVar('--s1', '#2a78d6'), s2: cssVar('--s2', '#eb6834'), s3: cssVar('--s3', '#1baf7a'),
                          up: cssVar('--up', '#e34948'), down: cssVar('--down', '#008300'), ink: cssVar('--ink', '#1d2433'),
    series: [['--s1','#2a78d6'],['--s2','#eb6834'],['--s3','#1baf7a'],['--s4','#eda100'],['--s5','#e87ba4'],['--s6','#008300'],['--s7','#4a3aa7'],['--s8','#e34948']].map(([n, f]) => cssVar(n, f))});
  function build(spec, mobile = false) {
    const P = palette();
    const v = spec.view, cols = Object.fromEntries(spec.columns.map(c => [c.key,c]));
    const field = slot => cols[v[slot]];
    const title = slot => { const c=field(slot); return c.label+(c.unit?' / '+c.unit:''); };
    const item = slot => ({field: v[slot], name: field(slot).label, valueFormatter: value => number(value)+(field(slot).unit?' '+field(slot).unit:'')});
    const data = spec.rows.map(r => ({...r.values, _row: r.id}));
    const label = slot => Object.fromEntries(data.map(d => [d._row,String(d[v[slot]] ?? '未提供')]));
    const base = {theme:isDark()?'classicDark':'classic', animate:false, padding:'auto', margin:12,
      interaction:{tooltip:{disableAutoHide:mobile},legendFilter:true},
      axis:{x:{title:false},y:{title:false}}};
    const one = (options) => {
      const result={...base,...options};
      const transposed=options.coordinate?.transform?.some(t=>t.type==='transpose');
      // G2's default left title rotates and shifts outside a zero-margin canvas.
      // Keep Chinese measure names upright, with outer space for the full glyph.
      if(!transposed && result.axis?.y?.title) result.axis={...result.axis,
        y:{...result.axis.y,titleTransform:'rotate(0)',titleDirection:'horizontal'}};
      return result;
    };
    const tip = (slot, slots) => ({title:v[slot],items:slots.map(item)});
    const k = spec.template;
    if (!chartKinds.includes(k)) return null;
    if (k==='comparison') {
      const names=label('label');
      return one({type:'interval',data:data.filter(d=>finite(d[v.value])).sort((a,b)=>b[v.value]-a[v.value]),
        coordinate:{transform:[{type:'transpose'}]},encode:{x:'_row',y:v.value},
        axis:{x:{title:false,labelFormatter:id=>names[id],labelAutoEllipsis:true},y:{title:title('value')}},
        style:{fill:P.s1,radius:4},labels:[{text:v.value,position:'right',style:{fill:P.ink}}],tooltip:tip('label',['value'])});
    }
    if (k==='trend') {
      const ordered=data.sort((a,b)=>a[v.x].localeCompare(b[v.x]));
      return one({type:'view',data:ordered,scale:{x:{type:'point'}},
        axis:{x:{title:false},y:{title:title('value')}},
        children:[{type:'line',encode:{x:v.x,y:v.value},style:{stroke:P.s1,lineWidth:2,connect:false},tooltip:tip('x',['value'])},
          {type:'point',data:ordered.filter(d=>finite(d[v.value])),encode:{x:v.x,y:v.value},style:{fill:P.s1,r:4},tooltip:tip('x',['value'])}]});
    }
    if (k==='composition') {
      const names=label('label');
      return one({type:'interval',data,coordinate:{type:'theta',innerRadius:0.6},
        transform:[{type:'stackY'}],encode:{y:v.value,color:'_row'},scale:{color:{range:P.series}},
        legend:{color:{position:'bottom',title:false,labelFormatter:id=>names[id] ?? id}},
        axis:false,tooltip:tip('label',['value'])});
    }
    if (k==='distribution') {
      const xs=data.map(d=>d[v.value]).filter(finite), lo=Math.min(...xs),hi=Math.max(...xs);
      if (!xs.length) return null;
      const n=lo===hi?1:Math.min(12,Math.max(3,Math.ceil(Math.sqrt(xs.length))));
      const rawStep=(hi-lo)/n, magnitude=rawStep?10**Math.floor(Math.log10(rawStep)):1;
      const step=rawStep?Math.ceil(rawStep/magnitude)*magnitude:0;
      const bins=Array.from({length:n},(_,i)=>({_row:String(i),start:lo+i*step,end:lo+(i+1)*step,count:0}));
      for(const x of xs) bins[step===0?0:Math.min(n-1,Math.floor((x-lo)/step))].count++;
      const range=d=>`${number(d.start)} ≤ 值 ${d===bins[n-1]?'≤':'<'} ${number(d.end)}${field('value').unit?' '+field('value').unit:''}`;
      for(const b of bins)b.range=range(b);
      return one({type:'interval',data:bins,encode:{x:'_row',y:'count'},
        axis:{x:{title:title('value'),labelFormatter:id=>String(Number(bins[+id].start.toPrecision(4)))},y:{title:'数量',tickMinStep:1}},
        style:{fill:P.s1,radius:4},tooltip:{title:'range',items:[{field:'count',name:'数量'}]}});
    }
    if (k==='relationship') return one({type:'point',data:data.filter(d=>finite(d[v.x])&&finite(d[v.y])),
      encode:{x:v.x,y:v.y},axis:{x:{title:title('x')},y:{title:title('y')}},
      style:{fill:P.s1,r:5,fillOpacity:0.8},tooltip:tip('label',['x','y'])});
    if (k==='matrix') return one({type:'cell',data:data.filter(d=>finite(d[v.value])),
      encode:{x:v.x,y:v.y,color:v.value},scale:{color:{palette:'blues'}},
      axis:{x:{title:title('x')},y:{title:title('y')}},legend:{color:{title:title('value')}},
      style:{inset:2},tooltip:{title:v.x,items:[{field:v.y,name:field('y').label},item('value')]}});
    if (k==='waterfall') {
      let sum=0;const names=label('label');
      for(const d of data){d._start=sum;sum+=d[v.value];d._end=sum;d._direction=d[v.value]>=0?'增加':'减少';}
      return one({type:'interval',data,encode:{x:'_row',y:['_start','_end'],color:'_direction'},
        scale:{color:{domain:['增加','减少'],range:[P.s1,P.s2]}},
        axis:{x:{title:false,labelFormatter:id=>names[id]},y:{title:title('value')}},legend:{color:{title:false}},
        tooltip:{title:v.label,items:[item('value'),{field:'_end',name:'累计',valueFormatter:x=>number(x)+(field('value').unit?' '+field('value').unit:'')}]}});
    }
    if (k==='candlestick') {
      for(const d of data)d._direction=d[v.close]>=d[v.open]?'上涨或持平':'下跌';
      data.sort((a,b)=>a[v.x].localeCompare(b[v.x]));
      const encode={x:v.x,color:'_direction'}, tooltip=tip('x',['open','high','low','close']);
      return one({type:'view',data,scale:{color:{domain:['上涨或持平','下跌'],range:[P.up,P.down]},y:{zero:false}},
        axis:{x:{title:false},y:{title:field('close').unit || false}},legend:{color:{title:false}},
        children:[{type:'link',encode:{...encode,y:[v.low,v.high]},style:{lineWidth:1.5},tooltip},
          {type:'interval',encode:{...encode,y:[v.open,v.close]},style:{maxWidth:22},tooltip}]});
    }
    if (k==='gantt') {
      const names=label('label');
      for(const d of data){d._start=Date.parse(d[v.start]+'T00:00:00Z');d._end=Date.parse(d[v.end]+'T00:00:00Z')+86400000;}
      return one({type:'interval',data,coordinate:{transform:[{type:'transpose'}]},encode:{x:'_row',y:['_start','_end']},
        scale:{y:{zero:false}},style:{fill:P.s1,radius:4},
        axis:{x:{title:false,labelFormatter:id=>names[id]},y:{title:false,labelFormatter:x=>new Date(Number(x)).toISOString().slice(5,10)}},
        tooltip:{title:v.label,items:[{field:v.start,name:field('start').label},{field:v.end,name:field('end').label}]}});
    }
  }
  async function mount(container,spec) {
    const mobile=matchMedia('(pointer:coarse)').matches;
    const options=build(spec,mobile);
    if(!options){container.textContent='本次没有可绘制的已知数值，请查看明细。';return;}
    const chart=new root.G2.Chart({container,autoFit:true,height:Math.max(340,
      ['gantt','comparison'].includes(spec.template)?Math.min(760,spec.rows.length*34+90):380)});
    container.loomChart=chart;
    chart.options(options);
    await chart.render();
    container.dataset.ready='true';
    // On touch devices preserve the native tooltip after lifting the finger.
    // A tap outside the figure or Escape dismisses it without losing the data.
    document.addEventListener('pointerdown',e=>{if(!container.contains(e.target))chart.emit('tooltip:hide');});
    document.addEventListener('keydown',e=>{if(e.key==='Escape')chart.emit('tooltip:hide');});
    return chart;
  }
  root.LOOMChart={build,mount,chartKinds};
  if(typeof module!=='undefined')module.exports=root.LOOMChart;
})(typeof window!=='undefined'?window:globalThis);
