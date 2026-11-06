"""Read-only metadata export and generic native Shortcuts builder."""
import json,re,plistlib,uuid,hashlib
from pathlib import Path
BASE=Path(__file__).parent
ROOT=Path.home()/'Desktop/03-内容创作/16-飞书HTML'
html=(ROOT/'00-HTML资料库管理器.html').read_text()
data=json.loads(re.search(r'<script id="snapshot" type="application/json">(.*?)</script>',html,re.S).group(1))
records=[]
clean=lambda s:re.sub(r'[\t\r\n]+',' ',s).strip()
for r in data['records']:
 p=Path(r['path'])
 if p.is_relative_to(ROOT) and p.exists() and p.name!='00-HTML资料库管理器.html':
  records.append((clean(r['title']),clean(r['snippet'])[:160],str(p.relative_to(ROOT))))
lines=['\t'.join(r) for r in records]
(BASE/'资料搜索索引.txt').write_text('\n'.join(lines)+'\n')
pilot=[]
for q in ['李笑来','租房','商业','学习','Agent']:
 for row in records:
  if q.lower() in '\t'.join(row).lower() and row not in pilot:
   pilot.append(row)
   if sum(q.lower() in '\t'.join(x).lower() for x in pilot)>=2:break
(BASE/'试用索引.txt').write_text('\n'.join('\t'.join(r) for r in pilot)+'\n')
def token(u,name='Text'):
 return {'WFSerializationType':'WFTextTokenAttachment','Value':{'Type':'ActionOutput','OutputUUID':u,'OutputName':name}}
def texttoken(prefix,u,suffix=''):
 return {'WFSerializationType':'WFTextTokenString','Value':{'string':prefix+'\ufffc'+suffix,'attachmentsByRange':{'{%d, 1}'%(len(prefix.encode('utf-16-le'))//2):token(u)['Value']}}}
def workflow(test=False):
 acts=[]
 def add(id,**params):
  u=str(uuid.uuid4()).upper();params['UUID']=u
  acts.append({'WFWorkflowActionIdentifier':'is.workflow.actions.'+id,'WFWorkflowActionParameters':params});return u
 ask={'WFSerializationType':'WFTextTokenAttachment','Value':{'Type':'Ask'}}
 if test:
  f=add('gettext',WFTextActionText='普通记录\t介绍\ta.html\n测试[AI].*中文\t介绍\t中文 空格.html\n其他记录\t介绍\tc.html')
  query=add('gettext',WFTextActionText='[AI].*')
 else:
  add('comment',WFCommentActionText='只读搜索：请选择16-飞书HTML文件夹，再输入一个关键词。文件夹内必须有资料搜索索引.txt。结果只搜索标题、摘要和文件名。选中结果后直接预览原文件，无需再选文件夹。不会修改文件。每次先选16-飞书HTML文件夹。')
  folder=add('file.select',WFPickingMode='Folders',SelectMultiple=False)
  f=add('documentpicker.open',WFFile=token(folder),WFGetFilePath='资料搜索索引.txt',WFFileErrorIfNotFound=True,WFGetFolderContents=False)
  f=add('detect.text',WFInput=token(f))
  query=add('ask',WFAskActionPrompt='输入一个关键词（标题 / 摘要 / 文件名）',WFInputType='Text',WFAllowsMultilineText=False)
 escaped=add('text.replace',WFInput=texttoken('',query),WFReplaceTextFind='\\E',WFReplaceTextReplace='\\E\\\\E\\Q',WFReplaceTextRegularExpression=False,WFReplaceTextCaseSensitive=True)
 match=add('text.match',text=texttoken('',f),WFMatchTextPattern=texttoken('(?im)^[^\\r\\n]*\\Q',escaped,'\\E[^\\r\\n]*$'),WFMatchTextCaseSensitive=False)
 if test:
  add('gettext',WFTextActionText=texttoken('',match))
 else:
  g=str(uuid.uuid4()).upper()
  add('conditional',WFControlFlowMode=0,GroupingIdentifier=g,WFCondition=101,WFInput={'Type':'Variable','Variable':token(match)})
  add('showresult',Text='没有匹配。请换一个关键词；当前搜索标题、摘要和文件名。')
  add('exit')
  add('conditional',WFControlFlowMode=2,GroupingIdentifier=g)
  choice=add('choosefromlist',WFInput=token(match),WFChooseFromListActionPrompt='选择资料（标题 / 摘要 / 文件名）',WFChooseFromListActionSelectMultiple=False)
  fields=add('text.split',text=texttoken('',choice),WFTextSeparator='Custom',WFTextCustomSeparator='\t')
  path=add('getitemfromlist',WFInput=token(fields),WFItemSpecifier='Last Item')
  file=add('documentpicker.open',WFFile=token(folder),WFGetFilePath=texttoken('',path),WFFileErrorIfNotFound=True,WFGetFolderContents=False)
  add('previewdocument',WFInput=token(file))
 return {'WFWorkflowMinimumClientVersion':900,'WFWorkflowMinimumClientVersionString':'900','WFWorkflowClientVersion':'3036.0.4','WFWorkflowIcon':{'WFWorkflowIconStartColor':4282601983,'WFWorkflowIconGlyphNumber':61440},'WFWorkflowActions':acts,'WFWorkflowInputContentItemClasses':['WFStringContentItem','WFGenericFileContentItem'],'WFWorkflowTypes':[],'WFWorkflowImportQuestions':[],'WFQuickActionSurfaces':[],'WFWorkflowHasShortcutInputVariables':False,'WFWorkflowOutputContentItemClasses':[]}
for name,test in [('飞书资料搜索-最终版',False)]:
 (BASE/(name+'.unsigned.shortcut')).write_bytes(plistlib.dumps(workflow(test),fmt=plistlib.FMT_BINARY))
print(json.dumps({'records':len(records),'pilot':len(pilot),'index_bytes':(BASE/'资料搜索索引.txt').stat().st_size},ensure_ascii=False))
