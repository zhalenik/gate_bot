'use strict';
(() => {
  const data=document.getElementById('helpData');if(!data)return;
  const articles=JSON.parse(data.textContent),root=document.getElementById('helpArticles'),nav=document.getElementById('helpNavigation'),search=document.getElementById('helpSearch');
  const node=(tag,text='',className='')=>{const value=document.createElement(tag);value.textContent=text;value.className=className;return value;};
  const groups=[...new Set(articles.map(article=>article.group))];let group='';
  const all=node('button','Все инструкции','help-category');all.type='button';all.onclick=()=>{group='';render();};nav.append(all);
  for(const title of groups){const button=node('button',title,'help-category');button.type='button';button.onclick=()=>{group=title;render();};nav.append(button);}
  function render(){
    const words=search.value.trim().toLocaleLowerCase('ru').split(/\s+/).filter(Boolean);
    const visible=articles.filter(article=>(!group||article.group===group)&&words.every(word=>(article.title+' '+article.summary+' '+article.steps.join(' ')).toLocaleLowerCase('ru').includes(word)));
    root.replaceChildren();document.getElementById('helpEmpty').hidden=!!visible.length;
    for(const button of nav.children)button.setAttribute('aria-pressed',String((button===all&&!group)||button.textContent===group));
    for(const article of visible){
      const details=node('details','','help-article panel');details.id='help-'+article.id;
      const summary=node('summary');summary.append(node('span',article.group,'help-group'),node('strong',article.title),node('span',article.summary,'page-description'));details.append(summary);
      const steps=node('ol');for(const text of article.steps)steps.append(node('li',text));details.append(steps);
      const footer=node('div','','help-related');
      if(article.href){const link=node('a','Открыть раздел →','btn-action');link.href=article.href;footer.append(link);}
      for(const related of article.related){const target=articles.find(item=>item.id===related);if(!target)continue;const link=node('button',target.title,'text-link');link.type='button';link.onclick=()=>{group='';search.value='';render();const item=document.getElementById('help-'+related);item.open=true;item.scrollIntoView({block:'start',behavior:'smooth'});};footer.append(link);}
      details.append(footer);root.append(details);
    }
  }
  search.addEventListener('input',render);render();
})();
