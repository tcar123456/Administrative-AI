import {mkdir, copyFile, readdir, readFile, writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {resolve} from 'node:path';
await mkdir('.generated/public/static', {recursive:true});
for(const name of ['app.js','settings.js','calendar.js','style.css'])await copyFile('static/'+name,'.generated/public/static/'+name);
await copyFile('static/index.html','.generated/public/index.html');
const docs=[];
const directory=resolve(process.env.KNOWLEDGE_DIR||'knowledge');
for(const name of (await readdir(directory)).sort()){
  if(!/\.(md|txt)$/i.test(name))continue;
  const text=await readFile(resolve(directory,name),'utf8');
  const fingerprint=createHash('sha256').update(text).digest('hex');
  const title=text.split('\n')[0].replace(/^#+\s*/,'').trim()||name;
  const chunks=[];
  for(let i=0;i<text.length;i+=550){const part=text.slice(i,i+650).trim();if(part)chunks.push({source:name,title,page:null,chunk:i/550+1,text:part,fingerprint});}
  if(chunks.length)docs.push({source:name,title,fingerprint,chunks});
}
if(!docs.length)throw new Error('KNOWLEDGE_DIR has no readable Markdown/text documents');
await writeFile('.generated/knowledge.json',JSON.stringify(docs));
console.log(`Built static assets and ${docs.length} private knowledge documents.`);
