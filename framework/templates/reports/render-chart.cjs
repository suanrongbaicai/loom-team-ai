// Apache ECharts official example structures in a fixed Tabler report card.
const fs=require('fs');
const echarts=require('./vendor/echarts.min.js');
const s=JSON.parse(fs.readFileSync(0,'utf8')), v=s.view, rs=s.rows;
const value=(row,key)=>row.values[v[key]];
const blue='#066fd1', teal='#2fb344', orange='#f59f00', gray='#667382';
let opt={animation:false,color:[blue,teal,orange,'#ae3ec9','#d63939','#17a2b8'],backgroundColor:'#fff',textStyle:{fontFamily:'Microsoft YaHei, sans-serif',fontSize:14},grid:{left:65,right:30,top:25,bottom:60,containLabel:true}};
let title='', height=360, rows=[...rs], labels;
const numberAxis=(name)=>({type:'value',name:name||'',nameTextStyle:{color:gray},splitLine:{lineStyle:{color:'#edf0f4'}},axisLabel:{color:gray}});
const categoryAxis=(data)=>({type:'category',data,axisLine:{lineStyle:{color:'#dde2ea'}},axisTick:{show:false},axisLabel:{color:gray,hideOverlap:true}});
switch(s.template){
case 'comparison':
 rows.sort((a,b)=>(value(a,'value')===null)-(value(b,'value')===null)||(value(b,'value')??0)-(value(a,'value')??0));
 title='对象比较';height=Math.max(340,Math.min(2200,rows.length*38+80));
 opt.xAxis=numberAxis();opt.yAxis={...categoryAxis(rows.map(r=>String(value(r,'label')))),inverse:true,axisLabel:{color:gray,width:150,overflow:'truncate'}};
 opt.series=[{type:'bar',data:rows.map(r=>value(r,'value')),barMaxWidth:22,label:{show:true,position:'right'},itemStyle:{borderRadius:[0,3,3,0]}}];break;
case 'trend':
 title='随时间变化';rows.sort((a,b)=>String(value(a,'x')).localeCompare(String(value(b,'x'))));
 opt.xAxis=categoryAxis(rows.map(r=>value(r,'x')));opt.yAxis=numberAxis();
 opt.series=[{type:'line',smooth:false,connectNulls:false,symbolSize:6,data:rows.map(r=>value(r,'value')),lineStyle:{width:3},areaStyle:{opacity:.05}}];break;
case 'composition':
 title='构成占比';opt.legend={bottom:0,textStyle:{fontSize:13}};
 opt.series=[{type:'pie',radius:['43%','67%'],center:['50%','44%'],label:{show:true,formatter:'{b}\n{d}%'},data:rows.map(r=>({name:String(value(r,'label')),value:value(r,'value')}))}];break;
case 'distribution':{
 title='数值分布';const spec=require('./antv-report.js').build(s);
 if(!spec)throw Error('No observed distribution values');
 const counts=spec.data.map(d=>d.count);labels=spec.data.map(d=>d.start+'–'+d.end);
 opt.xAxis=categoryAxis(labels);opt.yAxis={...numberAxis('条数'),minInterval:1};opt.series=[{type:'bar',data:counts,barCategoryGap:'3%',label:{show:true,position:'top'}}];break;}
case 'relationship':
 title='两项数值的关系';opt.xAxis=numberAxis(s.columns.find(c=>c.key===v.x).label);opt.yAxis=numberAxis(s.columns.find(c=>c.key===v.y).label);
 opt.series=[{type:'scatter',symbolSize:11,itemStyle:{opacity:.72},data:rows.filter(r=>value(r,'x')!==null&&value(r,'y')!==null).map(r=>({value:[value(r,'x'),value(r,'y')],name:String(value(r,'label'))}))}];break;
case 'matrix':{
 title='交叉热力表';let xs=[...new Set(rows.map(r=>String(value(r,'x'))))],ys=[...new Set(rows.map(r=>String(value(r,'y'))))];
 opt.xAxis=categoryAxis(xs);opt.yAxis=categoryAxis(ys);let values=rows.filter(r=>value(r,'value')!==null).map(r=>[xs.indexOf(String(value(r,'x'))),ys.indexOf(String(value(r,'y'))),value(r,'value')]);
 opt.visualMap={min:Math.min(...values.map(r=>r[2])),max:Math.max(...values.map(r=>r[2])),show:false,inRange:{color:['#eff6ff',blue]}};
 opt.series=[{type:'heatmap',data:values,label:{show:true},itemStyle:{borderColor:'#fff',borderWidth:3}}];break;}
case 'waterfall':{
 title='增减与终值';let running=0;let base=[],up=[],down=[];labels=rows.map(r=>String(value(r,'label')));
 for(const r of rows){let n=value(r,'value'),next=running+n;base.push(Math.min(running,next));up.push(n>=0?n:'-');down.push(n<0?-n:'-');running=next;}
   // Fixed range rectangles also support crossing zero in the native document export.
   if(base.some(n=>n<0)||running<0){
     let sum=0;const ranges=rows.map((r,i)=>{const start=sum;sum+=value(r,'value');return [i,start,sum,value(r,'value')];});
     labels.push('终值');ranges.push([rows.length,0,sum,sum]);opt.xAxis=categoryAxis(labels);opt.yAxis=numberAxis();
     opt.series=[{type:'custom',data:ranges,encode:{x:0,y:[1,2]},renderItem:(params,api)=>{
       const a=api.coord([api.value(0),api.value(1)]),b=api.coord([api.value(0),api.value(2)]),w=api.size([1,0])[0]*0.6;
       return {type:'rect',shape:{x:a[0]-w/2,y:Math.min(a[1],b[1]),width:w,height:Math.abs(a[1]-b[1])},style:{fill:api.value(3)>=0?blue:'#d63939'}};
     }}];break;
   }
 labels.push('终值');base.push(0);up.push(running);down.push('-');opt.xAxis=categoryAxis(labels);opt.yAxis=numberAxis();
 opt.series=[{type:'bar',stack:'sum',data:base,itemStyle:{color:'transparent'},emphasis:{disabled:true}}, {name:'增加',type:'bar',stack:'sum',data:up,label:{show:true,position:'top'}},{name:'减少',type:'bar',stack:'sum',data:down,itemStyle:{color:'#d63939'},label:{show:true,position:'top'}}];break;}
case 'candlestick':
 title='区间开高低收';rows.sort((a,b)=>String(value(a,'x')).localeCompare(String(value(b,'x'))));opt.xAxis=categoryAxis(rows.map(r=>value(r,'x')));opt.yAxis={...numberAxis(),scale:true};
 opt.series=[{type:'candlestick',data:rows.map(r=>['open','close','low','high'].map(k=>value(r,k))),itemStyle:{color:'#d63939',color0:teal,borderColor:'#d63939',borderColor0:teal}}];break;
default:throw Error('Unknown fixed chart');
}
function draw(width,option){const chart=echarts.init(null,null,{renderer:'svg',ssr:true,width,height});chart.setOption(option);const svg=chart.renderToSVGString();chart.dispose();return svg;}
const svg=draw(680,opt);
const copyOptions=x=>Array.isArray(x)?x.map(copyOptions):x&&typeof x==='object'?Object.fromEntries(Object.entries(x).map(([k,v])=>[k,copyOptions(v)])):x;
const mobile=copyOptions(opt);mobile.grid={left:8,right:18,top:35,bottom:45,containLabel:true};
if(mobile.yAxis?.type==='category')mobile.yAxis.axisLabel={...mobile.yAxis.axisLabel,width:90,overflow:'truncate'};
if(s.template==='composition'){mobile.series[0].label={show:false};mobile.legend={bottom:0,textStyle:{fontSize:13}};}
if(s.template==='relationship'){mobile.xAxis.name='';mobile.yAxis.name='';}
const svg_mobile=draw(340,mobile);let png=null;
if(process.env.ANC_RESVG_MODULE){const {Resvg}=require(process.env.ANC_RESVG_MODULE);png=new Resvg(svg_mobile,{fitTo:{mode:'width',value:1020}}).render().asPng().toString('base64');}
process.stdout.write(JSON.stringify({title,svg,svg_mobile,png}));
