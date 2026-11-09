(() => {
  const el = document.getElementById('cashflowChart');
  if (!el || !window.Chart) return;
  const rows = window.EDA_CHART || [];
  new Chart(el, {
    type: 'bar',
    data: {labels: rows.map(x=>x.label), datasets:[
      {label:'Příjmy', data:rows.map(x=>x.income), backgroundColor:'#9EC415', borderRadius:6},
      {label:'Náklady', data:rows.map(x=>x.expense), backgroundColor:'#E40058', borderRadius:6}
    ]},
    options:{responsive:true, maintainAspectRatio:false, plugins:{legend:{position:'bottom', labels:{usePointStyle:true, boxWidth:8, font:{family:'Poppins'}}}}, scales:{x:{grid:{display:false}},y:{beginAtZero:true, ticks:{callback:v=>new Intl.NumberFormat('cs-CZ').format(v)+' Kč'}}}}
  });
})();
