"use strict";

function timestamp(value) {
  if (!value) return null;
  const number = Date.parse(value);
  return Number.isFinite(number) ? number : null;
}

function effectiveDeadline(a, now) {
  const original = timestamp(a.deadline_at), late = timestamp(a.late_deadline_at);
  return late !== null && (original === null || (now >= original && late > original)) ? late : original;
}

function taskState(a, now) {
  const deadline = effectiveDeadline(a, now), original = timestamp(a.deadline_at), starts = timestamp(a.starts_at);
  const diff = deadline === null ? null : (deadline - now) / 1000;
  const remaining = diff === null ? null : Math.sign(diff) * Math.ceil(Math.abs(diff));
  const isLate = deadline !== null && deadline === timestamp(a.late_deadline_at) && (original === null || (now >= original && deadline > original));
  let state, urgency;
  if (a.submission_status === "submitted") { state = "submitted"; urgency = "done"; }
  else if (deadline !== null && now >= deadline) { state = "overdue"; urgency = "overdue"; }
  else if (a.availability === "closed" && !isLate) { state = "closed"; urgency = "normal"; }
  else if (deadline === null) { state = "unknown"; urgency = "unknown"; }
  else {
    state = (starts !== null ? starts > now : a.availability === "not_started") ? "not_started" : "pending";
    urgency = remaining <= 86400 ? "urgent" : remaining <= 259200 ? "soon" : "normal";
  }
  return {state, deadline, remaining, urgency, isLate};
}

function countdown(value, now, compact = false) {
  if (value === null || !Number.isFinite(value)) return "截止时间未知";
  const diff = value - now;
  if (diff === 0) return "已截止";
  let n = Math.ceil(Math.abs(diff) / 1000);
  const d = Math.floor(n / 86400); n %= 86400;
  const h = Math.floor(n / 3600); n %= 3600;
  const m = Math.floor(n / 60), s = n % 60;
  const parts = [];
  if (d) parts.push(d + "天");
  if (d || h) parts.push(h + "小时");
  if (!compact || !d) parts.push(m + "分");
  if (!compact || (!d && !h)) parts.push(s + "秒");
  return (diff > 0 ? "剩余 " : "已逾期 ") + parts.join(" ");
}

function priorityAssignments(assignments, now) {
  return assignments.filter(a => {
    const state = taskState(a, now);
    return a.submission_status === "unsubmitted" && state.deadline !== null && state.deadline > now && state.state !== "closed";
  }).sort((a,b) => effectiveDeadline(a,now)-effectiveDeadline(b,now) || a.id.localeCompare(b.id));
}

function groupCourses(courses, assignments, now) {
  const map = new Map();
  for (const course of courses) map.set(String(course.id), {id:String(course.id), name:course.name_ja || "课程名称未提供", items:[]});
  for (const a of assignments) {
    const id = String(a.course_id || "unknown");
    if (!map.has(id)) map.set(id, {id, name:a.course_name_ja || "课程名称未提供", items:[]});
    const group = map.get(id);
    if (group.name === "课程名称未提供" && a.course_name_ja) group.name = a.course_name_ja;
    group.items.push(a);
  }
  const groups = [...map.values()], used = new Set();
  for (const group of [...groups].sort((a,b) => a.id.localeCompare(b.id))) {
    let hash = 0;
    for (const c of group.id) hash = ((hash * 31) + c.charCodeAt(0)) >>> 0;
    let color = hash % 12;
    for (let i = 0; used.has(color) && i < 12; i++) color = (color + 1) % 12;
    used.add(color); group.color = color;
    group.pending = group.items.filter(a => a.submission_status !== "submitted");
    group.submitted = group.items.filter(a => a.submission_status === "submitted");
    group.priority = priorityAssignments(group.items, now);
    const future = group.pending.filter(a => {const s = taskState(a,now); return s.deadline !== null && s.deadline > now && s.state !== "closed";});
    future.sort((a,b) => effectiveDeadline(a,now) - effectiveDeadline(b,now));
    group.next = group.priority[0] || future[0] || null;
    group.overdueCount = group.pending.filter(a => taskState(a,now).state === "overdue").length;
    group.unknownCount = group.pending.filter(a => taskState(a,now).state === "unknown").length;
    const priorityIds = new Set(group.priority.map(a=>a.id));
    const rank = a => {const s=taskState(a,now); return priorityIds.has(a.id)?0:s.state==="overdue"?2:s.state==="closed"?4:s.deadline===null?3:1;};
    group.pending.sort((a,b) => rank(a)-rank(b) || (effectiveDeadline(a,now)??Infinity)-(effectiveDeadline(b,now)??Infinity) || a.id.localeCompare(b.id));
    group.submitted.sort((a,b) => (effectiveDeadline(b,now)??-Infinity)-(effectiveDeadline(a,now)??-Infinity));
  }
  return groups.sort((a,b) => (b.priority.length>0)-(a.priority.length>0) || (a.next?effectiveDeadline(a.next,now):Infinity)-(b.next?effectiveDeadline(b.next,now):Infinity) || (b.overdueCount>0)-(a.overdueCount>0) || (b.pending.length>0)-(a.pending.length>0) || a.id.localeCompare(b.id));
}

if (typeof module !== "undefined" && module.exports) module.exports = {effectiveDeadline, taskState, groupCourses, countdown, priorityAssignments};

if (typeof document !== "undefined") {
  const token = document.querySelector('meta[name="local-token"]').content;
  const $ = id => document.getElementById(id);
  const dateFormatter = new Intl.DateTimeFormat("zh-CN", {year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23",timeZone:"Asia/Tokyo"});
  const shortFormatter = new Intl.DateTimeFormat("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23",timeZone:"Asia/Tokyo"});
  const todayFormatter = new Intl.DateTimeFormat("zh-CN", {month:"numeric",day:"numeric",weekday:"long",timeZone:"Asia/Tokyo"});
  let assignments = [], courses = [], status = {}, fingerprint = null, localMessage = "", messageUntil = 0;
  const taskNodes = new Map(), groupNodes = new Map(), priorityNodes = new Map();
  let priorityKey = null, selectedCourseId = null, detailPending = null, returnFocus = null;
  const date = value => {const ms=typeof value==="number"?value:timestamp(value); return ms===null?"截止时间未知":dateFormatter.format(new Date(ms));};
  const shortDate = value => shortFormatter.format(new Date(value));
  const AESPA_MEMBERS = [
    { id: "karina", name: "Karina", zh: "刘知珉", src: "/avatars/karina.jpg" },
    { id: "winter", name: "Winter", zh: "金旼炡", src: "/avatars/winter.jpg" },
    { id: "giselle", name: "Giselle", zh: "内永枝利", src: "/avatars/giselle.jpg" },
    { id: "ningning", name: "Ningning", zh: "宁艺卓", src: "/avatars/ningning.jpg" },
  ];
  function getMember(colorIndex) {
    const idx = Math.abs(Number(colorIndex) || 0) % AESPA_MEMBERS.length;
    return AESPA_MEMBERS[idx];
  }
  function courseAvatar(name, colorIndex) {
    const member = getMember(colorIndex);
    const avatar = element("div", "course-avatar");
    avatar.setAttribute("aria-label", member.name + " (" + member.zh + ")");
    avatar.title = member.name + " (" + member.zh + ") · " + name;
    const img = document.createElement("img");
    img.className = "avatar-img";
    img.src = member.src;
    img.alt = member.name;
    img.loading = "lazy";
    const fallback = element("span", "avatar-fallback", [...name][0] || "课");
    fallback.hidden = true;
    img.onerror = () => { img.hidden = true; fallback.hidden = false; };
    avatar.append(img, fallback);
    return avatar;
  }
  function courseCountText(group) {
    const parts=[];
    if(group.pending.length)parts.push("待处理 "+group.pending.length+" 项");
    if(group.submitted.length)parts.push("已提交 "+group.submitted.length+" 项");
    return parts.join(" · ");
  }
  function element(tag, className, text) { const node=document.createElement(tag); if(className)node.className=className; if(text!==undefined)node.textContent=text; return node; }
  async function api(path,method="GET",data) {
    const response = await fetch(path,{method,cache:"no-store",credentials:"omit",headers:{"X-Manaba-Token":token,...(data?{"Content-Type":"application/json"}:{})},body:data?JSON.stringify(data):undefined});
    const value=await response.json(); if(!response.ok)throw Error(value.message_zh||"本地服务请求失败。"); return value;
  }
  function showStatus() {
    const message=Date.now()<messageUntil?localMessage+" ":"";
    $("status-text").textContent=message+(status.message_zh||"")+(status.stale&&status.assignment_count?" 当前便签包含上次数据。":"");
    $("status").className="statusbar glass"+(status.stale?" old":"")+(status.state==="error"?" error":"");
    $("last-sync").textContent=status.last_success_at?"更新 "+shortDate(timestamp(status.last_success_at))+" · 日本时间":"日本时间";
    for(const id of ["login","sync","account"])$(id).disabled=Boolean(status.busy);
    $("scope").textContent=courses.length+" 门课";
  }
  function sourceLink(a) {
    try { const url=new URL(a.source_url); if(url.protocol==="https:" && url.hostname==="manaba.ryukoku.ac.jp" && !url.username && !url.password){const link=element("a","","去 manaba 查看 ↗");link.href=url.href;link.target="_blank";link.rel="noopener noreferrer";return link;} } catch {}
    return null;
  }
  function updatePriority(groups, now) {
    const items=priorityAssignments(assignments,now), colors=new Map(groups.map(g=>[g.id,g.color]));
    const key=JSON.stringify(items.map(a=>[a.id,effectiveDeadline(a,now)]));
    $("priority-list").hidden=!items.length;$("priority-empty").hidden=Boolean(items.length);
    $("priority-empty").textContent=courses.length||status.complete?"现在没有要赶的作业 ☁️ 去喝口水，歇一会儿吧。":"登录后，最近要交的作业会像消息一样出现在这里。";
    $("priority-count").textContent=items.length?items.length+" 条新消息":"都看完啦";
    $("priority-count").classList.toggle("read",!items.length);
    if(key!==priorityKey){
      priorityKey=key;priorityNodes.clear();const fragment=document.createDocumentFragment();
      for(const a of items){
        const color=colors.get(String(a.course_id))??0;
        const member=getMember(color);
        const root=element("article","message color-"+color);root.dataset.priorityAssignment=a.id;
        const name=a.course_name_ja||"课程名称未提供";
        const course=element("p","message-name");
        course.title=member.name+" ("+member.zh+") · "+name;
        course.append(element("span","member-tag",member.name),document.createTextNode(name));
        const title=element("h3","priority-title",a.title_ja||"标题未提供");
        title.title=title.textContent;
        const main=element("div","message-main"), row=element("div","message-row"), bubble=element("div","bubble");
        const clock=element("p","priority-clock"), deadline=element("p","priority-date"), stateLabel=element("p","message-meta");
        const link=element("button","bubble-link","看看作业 →");link.setAttribute("aria-haspopup","dialog");link.onclick=()=>showCourse(String(a.course_id),link);
        bubble.append(title,clock,deadline,link);row.append(bubble,stateLabel);main.append(course,row);
        root.append(courseAvatar(name,color),main);fragment.append(root);priorityNodes.set(a.id,{clock,deadline,stateLabel,button:link});
      }
      $("priority-list").replaceChildren(fragment);
    }
    for(const a of items){
      const node=priorityNodes.get(a.id), state=taskState(a,now);
      node.clock.textContent=countdown(state.deadline,now,true);node.clock.className="priority-clock "+state.urgency;
      node.deadline.textContent=(state.isLate?"迟交截止：":"截止：")+date(state.deadline);
      node.stateLabel.textContent="未提交"+(state.state==="not_started"?" · 尚未开放":state.isLate?" · 可迟交":"");
      if(status.stale||(a.issues||[]).some(i=>i.code==="snapshot_stale"))node.stateLabel.append(element("span","stale-tag","上次数据"));
    }
  }
  function taskElement(a, opened) {
    const root=element("article","task"); root.dataset.assignment=a.id;
    const heading=element("div","task-heading"), title=element("h3","task-title",a.title_ja||"标题未提供");
    heading.append(title,element("span","task-type",a.kind_zh||"作业"));
    const badge=element("p","task-status"), clock=element("p","task-clock"), deadline=element("p","task-date"), original=element("p","task-original");
    root.append(heading,badge,clock,deadline,original);
    const details=element("details","task-detail"); details.dataset.persist="task:"+a.id; details.open=opened.has(details.dataset.persist);
    details.append(element("summary","","作业说明"+(a.content_status==="loaded"?"":" · "+(a.content_status_zh||"状态未知"))),element("pre","",a.content_ja??"正文未读取或未提供。"));
    const link=sourceLink(a); if(link)details.append(link); root.append(details);
    taskNodes.set(a.id,{root,badge,clock,deadline,original}); return root;
  }
  function openedTaskDetails() {
    return new Set([...$("course-dialog").querySelectorAll("details[open]")].map(node=>node.dataset.persist));
  }
  function dialogFocusKey() {
    const active=document.activeElement;
    if(!$("course-dialog").open||!$("course-dialog").contains(active))return null;
    if(active.tagName==="SUMMARY")return {type:"summary",persist:active.parentElement.dataset.persist};
    const task=active.closest("[data-assignment]");
    if(active.tagName==="A"&&task)return {type:"source",assignmentId:task.dataset.assignment};
    return {type:"close"};
  }
  function restoreDialogFocus(key) {
    if(!key||!$("course-dialog").open)return;
    let target;
    if(key.type==="summary")target=[...$("course-dialog").querySelectorAll("details[data-persist]")].find(node=>node.dataset.persist===key.persist)?.querySelector("summary");
    else if(key.type==="source")target=taskNodes.get(key.assignmentId)?.root.querySelector("a");
    if(!target||!target.getClientRects().length)target=$("close-course");
    if(target!==document.activeElement)target.focus({preventScroll:true});
  }
  function renderCourseDialog(opened) {
    const focusKey=dialogFocusKey();
    const group=groupCourses(courses,assignments,Date.now()).find(g=>g.id===selectedCourseId);
    taskNodes.clear();detailPending=null;
    if(!group){if($("course-dialog").open)$("course-dialog").close();selectedCourseId=null;return;}
    $("course-dialog").className="course-dialog color-"+group.color;
    const member=getMember(group.color);
    const dlgAvatar=$("course-dialog-avatar");
    dlgAvatar.replaceChildren(courseAvatar(group.name,group.color).querySelector(".avatar-img")?.cloneNode(true)||element("span","", [...group.name][0]||"课"));
    dlgAvatar.title=member.name+" ("+member.zh+")";
    $("course-dialog-title").textContent=group.name;
    $("course-dialog-count").textContent=[member.name+" ("+member.zh+") 陪你学",courseCountText(group),"日本时间"].filter(Boolean).join(" · ");
    const body=$("course-detail-list"), fragment=document.createDocumentFragment();
    if(group.pending.length){
      fragment.append(element("h3","detail-section-title","待处理"));
      detailPending=element("div","pending-tasks");for(const a of group.pending)detailPending.append(taskElement(a,opened));fragment.append(detailPending);
    }
    if(group.submitted.length){
      const history=element("details","submitted-history");history.dataset.persist="history:"+group.id;history.open=opened.has(history.dataset.persist);
      history.append(element("summary","","已提交 "+group.submitted.length+" 项"));for(const a of group.submitted)history.append(taskElement(a,opened));fragment.append(history);
    }
    if(!group.items.length)fragment.append(element("p","empty-course",status.stale?"本次尚未读到作业，请留意同步提示。":"当前课程暂无作业。"));
    body.replaceChildren(fragment);restoreDialogFocus(focusKey);
  }
  function showCourse(id,trigger) {
    returnFocus={courseId:id,priorityId:trigger?.closest("[data-priority-assignment]")?.dataset.priorityAssignment||null};
    const opened=id===selectedCourseId?openedTaskDetails():new Set();selectedCourseId=id;
    renderCourseDialog(opened);if(selectedCourseId===null)return;
    if(!$("course-dialog").open)$("course-dialog").showModal();tick();
  }
  function render() {
    const opened=openedTaskDetails();
    taskNodes.clear();groupNodes.clear();priorityKey=null;const fragment=document.createDocumentFragment(), groups=groupCourses(courses,assignments,Date.now());
    groups.forEach((group,index)=>{
      const root=element("section","chat-row color-"+group.color);root.dataset.course=group.id;root.id="note-"+group.id;
      const title=element("h2","course-title",group.name); title.id="course-"+index; root.setAttribute("aria-labelledby",title.id);
      title.title=group.name;
      const main=element("div","chat-main"), badge=element("span","unread-badge");badge.hidden=true;
      const line=element("p","chat-line"), label=element("span","summary-label"), clock=element("span","course-clock");line.append(label,clock);
      const nextTitle=element("p","summary-title"), meta=element("p","chat-meta"), deadline=element("span","summary-date"), alert=element("span","course-alert");meta.append(deadline,alert);
      const count=element("p","course-count");
      const button=element("button","note-button",group.items.length?"打开 →":"暂无作业");button.disabled=!group.items.length;button.hidden=!group.items.length;button.setAttribute("aria-haspopup","dialog");button.setAttribute("aria-label","看看「"+group.name+"」的作业");button.onclick=()=>showCourse(group.id,button);
      const footer=element("div","chat-foot");footer.append(count,button);
      main.append(title,badge,line,nextTitle,meta,footer);root.append(courseAvatar(group.name,group.color),main);
      groupNodes.set(group.id,{root,badge,label,clock,nextTitle,deadline,alert,count,button});fragment.append(root);
    });
    if(!groups.length){const empty=element("div","empty-board");empty.append(element("h3","",status.complete?"这里暂时没有课程":"先登录，我们一起理一理"),element("p","",status.complete?"可以先歇一会儿。":"课程和截止时间会整理在这里。"));fragment.append(empty);}
    $("board").replaceChildren(fragment);if(selectedCourseId!==null)renderCourseDialog(opened);tick();
  }
  function tick() {
    const focusKey=dialogFocusKey();
    const now=Date.now(), groups=groupCourses(courses,assignments,now);
    $("today-label").textContent="今天 · "+todayFormatter.format(new Date(now));
    updatePriority(groups,now);
    let pendingTotal=0, urgentTotal=0, overdueTotal=0, submittedTotal=0;
    let courseIndex=0;
    for(const group of groups){
      pendingTotal+=group.pending.length;submittedTotal+=group.submitted.length;overdueTotal+=group.overdueCount;
      urgentTotal+=group.pending.filter(a=>{const s=taskState(a,now);return s.urgency==="urgent"&&s.state!=="closed";}).length;
      const node=groupNodes.get(group.id);if(!node)continue;
      if($("board").children[courseIndex]!==node.root)$("board").insertBefore(node.root,$("board").children[courseIndex]||null);
      courseIndex++;
      if(group.id===selectedCourseId&&detailPending)group.pending.forEach((a,index)=>{const task=taskNodes.get(a.id);if(task&&detailPending.children[index]!==task.root)detailPending.insertBefore(task.root,detailPending.children[index]||null);});
      node.count.textContent=courseCountText(group);
      node.badge.textContent=String(group.priority.length);node.badge.hidden=!group.priority.length;node.badge.title=group.priority.length+" 项待交";
      node.root.classList.toggle("has-unread",group.priority.length>0);
      const stale=status.stale||group.items.some(a=>(a.issues||[]).some(i=>i.code==="snapshot_stale"));
      if(group.next){const state=taskState(group.next,now);node.label.textContent=group.next.submission_status!=="unsubmitted"?"截止 · 提交状态待确认":state.isLate?"迟交截止":"最近截止";node.clock.textContent=countdown(state.deadline,now,true);node.clock.className="course-clock "+state.urgency;node.nextTitle.textContent=group.next.title_ja;node.deadline.textContent="截止 "+shortDate(state.deadline)+(stale?" · 上次数据":"");}
      else {node.label.textContent=group.pending.length?"待处理":"本课作业";node.clock.textContent=group.overdueCount?"已逾期 "+group.overdueCount+" 项":group.unknownCount?"截止待确认":group.pending.length?"作业已结束":group.submitted.length?"已提交":"暂无作业";node.clock.className="course-clock "+(group.overdueCount?"overdue":group.submitted.length&&!group.pending.length?"done":"");node.nextTitle.textContent=group.unknownCount?group.unknownCount+" 项截止时间未知":!group.pending.length&&group.submitted.length?"这些完成的事，都算数。":!group.items.length&&status.complete&&!status.stale?"这门课先放一放。":"";node.deadline.textContent=stale?"上次数据 · 请留意同步状态":"";}
      node.alert.textContent=group.next&&group.overdueCount?"另有 "+group.overdueCount+" 项已逾期":"";
      node.nextTitle.title=node.nextTitle.textContent;node.alert.title=node.alert.textContent;
    }
    for(const a of assignments){const node=taskNodes.get(a.id);if(!node)continue;const state=taskState(a,now), starts=timestamp(a.starts_at);
      const submission=a.submission_status==="submitted"?"已提交":a.submission_status==="unsubmitted"?"未提交":a.submission_status==="in_progress"?"进行中":"提交状态待确认";
      const unopened=starts!==null?starts>now:a.availability==="not_started";
      const availability=state.state==="not_started"||(state.state==="unknown"&&unopened)?"尚未开放":state.state==="overdue"?"已截止":state.state==="closed"?"平台已结束":state.isLate?"可迟交":a.availability==="open"?"可提交":"";
      node.badge.textContent=submission+(state.state!=="submitted"&&availability?" · "+availability:"");
      if((a.issues||[]).some(i=>i.code==="snapshot_stale"))node.badge.append(element("span","stale-tag","上次数据"));
      node.clock.textContent=state.state==="submitted"?"已提交":state.state==="closed"?"平台已结束":countdown(state.deadline,now);node.clock.className="task-clock "+state.urgency;
      node.deadline.textContent=state.deadline===null?"暂无可用的截止时间":(state.isLate?"迟交截止：":"截止：")+date(state.deadline);
      node.original.textContent=state.isLate&&a.deadline_at?"原截止："+date(a.deadline_at):unopened&&state.state!=="submitted"&&starts!==null?"开放："+date(starts):"";
    }
    for(const [id,value] of [["pending-total",pendingTotal],["urgent-total",urgentTotal],["overdue-total",overdueTotal],["submitted-total",submittedTotal]])$(id).textContent=courses.length||status.complete?String(value):"—";
    restoreDialogFocus(focusKey);
  }
  function updateIssues(issues) {
    const counts=new Map();for(const i of issues)counts.set(i.message_zh,(counts.get(i.message_zh)||0)+1);
    $("diagnostics").hidden=!issues.length;$("issue-count").textContent="同步提示 "+issues.length+" 条 · 展开查看";
    $("issues").textContent=[...counts].map(([message,n])=>message+(n>1?"（"+n+" 项）":"")).join("\n");
  }
  function dataFingerprint() {
    return JSON.stringify({courses,assignments:assignments.map(({remaining_seconds,countdown_zh,status_zh,deadline_display_zh,...source})=>source),stale:status.stale,complete:status.complete});
  }
  async function refresh() {
    try{const data=await api("/api/assignments");assignments=data.assignments||[];courses=data.courses||[];status=data.status||{};
      if(document.activeElement!==$("url"))$("url").value=status.url||"";
      showStatus();updateIssues(data.issues||[]);const next=dataFingerprint();if(next!==fingerprint){fingerprint=next;render();}else tick();
    }catch(error){$("status-text").textContent=error.message;$("status").className="statusbar glass old error";}
  }
  async function action(path,data) {
    try{const result=await api(path,"POST",data||{});localMessage=result.message_zh||"";messageUntil=Date.now()+5000;await refresh();}catch(error){localMessage=error.message;messageUntil=Date.now()+5000;showStatus();}
  }
  $("sync").onclick=()=>action("/api/sync");$("login").onclick=()=>action("/api/login");$("account").onclick=()=>action("/api/account",{url:$("url").value});
  $("close-course").onclick=()=>$("course-dialog").close();
  $("course-dialog").addEventListener("close",()=>{
    selectedCourseId=null;detailPending=null;taskNodes.clear();
    const origin=returnFocus;returnFocus=null;
    let target=origin&&(priorityNodes.get(origin.priorityId)?.button||groupNodes.get(origin.courseId)?.button);
    if(!target||target.disabled)target=!$("sync").disabled?$("sync"):document.querySelector("h1");
    if(target.tagName==="H1")target.tabIndex=-1;
    target.focus({preventScroll:true});
  });

  // Wallpaper: a personal preference only. Images stay in this browser (IndexedDB); nothing is sent to the server.
  const WALL_KEY="deadline-bubble:wallpaper", WALL_DB="deadline-bubble", WALL_DEFAULT={source:"preset",preset:"bubble",blur:0,veil:25};
  const PRESETS={
    bubble:{name:"泡泡",css:"radial-gradient(circle at 14% 22%,rgb(255 255 255 / .75) 0 70px,transparent 72px),radial-gradient(circle at 86% 14%,rgb(255 255 255 / .55) 0 110px,transparent 112px),radial-gradient(circle at 74% 80%,rgb(255 255 255 / .5) 0 150px,transparent 152px),radial-gradient(circle at 28% 88%,rgb(255 255 255 / .45) 0 54px,transparent 56px),linear-gradient(135deg,#e3d9ff 0%,#fbdcef 52%,#d9eeff 100%)"},
    peach:{name:"蜜桃汽水",css:"radial-gradient(circle at 78% 24%,rgb(255 255 255 / .6) 0 90px,transparent 92px),linear-gradient(160deg,#ffe1ec 0%,#ffd5c8 48%,#fff0c8 100%)"},
    mint:{name:"薄荷糖",css:"radial-gradient(circle at 22% 76%,rgb(255 255 255 / .55) 0 110px,transparent 112px),linear-gradient(160deg,#d6f6ec 0%,#dcefff 55%,#ece4ff 100%)"},
    sakura:{name:"樱花",css:"radial-gradient(circle at 80% 20%,rgb(255 255 255 / .7) 0 90px,transparent 92px),radial-gradient(circle at 15% 70%,rgb(255 255 255 / .45) 0 60px,transparent 62px),linear-gradient(180deg,#fff1f6 0%,#ffd2e4 100%)"},
    sky:{name:"晴空",css:"radial-gradient(circle at 20% 80%,rgb(255 255 255 / .6) 0 120px,transparent 122px),radial-gradient(circle at 85% 30%,rgb(255 255 255 / .5) 0 70px,transparent 72px),linear-gradient(180deg,#c9e4ff 0%,#eef6ff 100%)"},
    night:{name:"应援星夜",css:"radial-gradient(circle at 18% 24%,#fff 0 1.5px,transparent 2.5px),radial-gradient(circle at 72% 16%,#fff 0 1px,transparent 2px),radial-gradient(circle at 86% 62%,#fff 0 1.5px,transparent 2.5px),radial-gradient(circle at 40% 70%,#fff 0 1px,transparent 2px),radial-gradient(circle at 60% 40%,#ffd6f0 0 1.5px,transparent 2.5px),linear-gradient(165deg,#2a2152 0%,#4b3a8f 55%,#b26bb5 100%)"},
    lilac:{name:"淡紫",css:"linear-gradient(180deg,#f3efff 0%,#e4dbff 100%)"},
  };
  const clamp=(value,min,max,fallback)=>{const n=Number(value);return Number.isFinite(n)?Math.min(max,Math.max(min,n)):fallback;};
  function loadWallSettings() {
    try{const v=JSON.parse(localStorage.getItem(WALL_KEY)||"{}")||{};
      return {source:v.source==="image"?"image":"preset",preset:Object.hasOwn(PRESETS,v.preset)?v.preset:WALL_DEFAULT.preset,blur:clamp(v.blur,0,24,WALL_DEFAULT.blur),veil:clamp(v.veil,0,85,WALL_DEFAULT.veil)};
    }catch{return {...WALL_DEFAULT};}
  }
  let wallSettings=loadWallSettings(), wallUrl=null;
  const saveWallSettings=()=>{try{localStorage.setItem(WALL_KEY,JSON.stringify(wallSettings));}catch{}};
  function wallStore(mode,operation) {
    return new Promise((resolve,reject)=>{
      if(typeof indexedDB==="undefined"){reject(Error("indexedDB unavailable"));return;}
      const open=indexedDB.open(WALL_DB,1);
      open.onupgradeneeded=()=>open.result.createObjectStore("files");
      open.onerror=()=>reject(open.error);
      open.onsuccess=()=>{const db=open.result;let request;
        try{const tx=db.transaction("files",mode);request=operation(tx.objectStore("files"));
          tx.oncomplete=()=>{db.close();resolve(request?.result);};tx.onerror=tx.onabort=()=>{db.close();reject(tx.error);};
        }catch(error){db.close();reject(error);}};
    });
  }
  function wallNote(text,isError=false) {$("wallpaper-message").textContent=text;$("wallpaper-message").classList.toggle("error",isError);}
  function useWallImage(blob) {
    if(wallUrl)URL.revokeObjectURL(wallUrl);
    wallUrl=blob?URL.createObjectURL(blob):null;
  }
  function applyWallpaper() {
    const style=document.documentElement.style, image=wallSettings.source==="image"&&wallUrl;
    style.setProperty("--wallpaper",image?'url("'+wallUrl+'")':PRESETS[wallSettings.preset].css);
    style.setProperty("--wall-blur",wallSettings.blur+"px");
    style.setProperty("--wall-veil",String(wallSettings.veil/100));
    $("wallpaper-blur").value=String(wallSettings.blur);$("blur-value").textContent=wallSettings.blur+" px";
    $("wallpaper-veil").value=String(wallSettings.veil);$("veil-value").textContent=wallSettings.veil+"%";
    $("wallpaper-remove").disabled=!wallUrl;
    for(const swatch of $("wallpaper-presets").children){
      const id=swatch.dataset.wallpaper, active=id==="image"?Boolean(image):!image&&id===wallSettings.preset;
      swatch.setAttribute("aria-pressed",String(active));
      if(id==="image"){swatch.hidden=!wallUrl;if(wallUrl)swatch.firstChild.style.setProperty("--swatch",'url("'+wallUrl+'")');}
    }
  }
  function buildSwatches() {
    const fragment=document.createDocumentFragment();
    for(const [id,preset] of [...Object.entries(PRESETS),["image",{name:"我的图片",css:"none"}]]){
      const button=element("button","swatch"), chip=element("span","swatch-chip");chip.setAttribute("aria-hidden","true");
      chip.style.setProperty("--swatch",preset.css);button.dataset.wallpaper=id;button.type="button";
      button.append(chip,element("span","",preset.name));
      button.onclick=()=>{
        if(id==="image"){if(!wallUrl)return;wallSettings.source="image";}else{wallSettings.source="preset";wallSettings.preset=id;}
        saveWallSettings();applyWallpaper();
      };
      fragment.append(button);
    }
    $("wallpaper-presets").replaceChildren(fragment);
  }
  async function prepareImage(file) {
    if(!file||!/^image\//.test(file.type))throw Error("请选择一张图片（JPG、PNG、WebP 或 GIF）。");
    if(file.size>40*1024*1024)throw Error("图片有点大，请选 40 MB 以内的。");
    if(file.type==="image/gif"&&file.size<=8*1024*1024)return file;
    let bitmap;
    try{bitmap=await createImageBitmap(file);}catch{throw Error("这张图片读不出来，换一张 JPG 或 PNG 试试。");}
    const scale=Math.min(1,2560/Math.max(bitmap.width,bitmap.height));
    const canvas=document.createElement("canvas");canvas.width=Math.max(1,Math.round(bitmap.width*scale));canvas.height=Math.max(1,Math.round(bitmap.height*scale));
    const context=canvas.getContext("2d");context.fillStyle="#fff";context.fillRect(0,0,canvas.width,canvas.height);
    context.drawImage(bitmap,0,0,canvas.width,canvas.height);bitmap.close?.();
    return new Promise((resolve,reject)=>canvas.toBlob(blob=>blob?resolve(blob):reject(Error("图片处理失败，换一张试试。")),"image/jpeg",.9));
  }
  async function setWallImage(file) {
    wallNote("正在换上新背景…");
    let blob;
    try{blob=await prepareImage(file);}catch(error){wallNote(error.message,true);return;}
    useWallImage(blob);wallSettings.source="image";saveWallSettings();applyWallpaper();
    try{await wallStore("readwrite",store=>store.put(blob,"wallpaper"));wallNote("换好啦 ♡ 背景只保存在这台电脑的浏览器里，不会上传。");}
    catch{wallNote("这次可以用，但浏览器没能保存，下次打开会恢复成预设。",true);}
  }
  async function removeWallImage() {
    useWallImage(null);wallSettings.source="preset";saveWallSettings();applyWallpaper();
    try{await wallStore("readwrite",store=>store.delete("wallpaper"));wallNote("已移除你的图片。");}catch{wallNote("已移除，本次打开期间不再使用这张图片。");}
  }
  buildSwatches();applyWallpaper();
  wallStore("readonly",store=>store.get("wallpaper")).then(blob=>{
    if(wallUrl)return;
    if(blob instanceof Blob){useWallImage(blob);}else if(wallSettings.source==="image"){wallSettings.source="preset";saveWallSettings();}
    applyWallpaper();
  }).catch(()=>{if(wallSettings.source==="image"&&!wallUrl){wallSettings.source="preset";applyWallpaper();}});
  $("wallpaper-open").onclick=()=>{if(!$("wallpaper-dialog").open)$("wallpaper-dialog").showModal();};
  $("wallpaper-close").onclick=$("wallpaper-done").onclick=()=>$("wallpaper-dialog").close();
  $("wallpaper-dialog").addEventListener("close",()=>$("wallpaper-open").focus({preventScroll:true}));
  $("wallpaper-upload").onclick=()=>$("wallpaper-file").click();
  $("wallpaper-file").onchange=()=>{const file=$("wallpaper-file").files[0];$("wallpaper-file").value="";if(file)setWallImage(file);};
  $("wallpaper-remove").onclick=removeWallImage;
  $("wallpaper-reset").onclick=()=>{wallSettings={...WALL_DEFAULT};saveWallSettings();applyWallpaper();wallNote("已恢复默认背景。"+(wallUrl?"你的图片还在，点「我的图片」可以换回来。":""));};
  $("wallpaper-blur").oninput=()=>{wallSettings.blur=clamp($("wallpaper-blur").value,0,24,0);applyWallpaper();};
  $("wallpaper-veil").oninput=()=>{wallSettings.veil=clamp($("wallpaper-veil").value,0,85,25);applyWallpaper();};
  $("wallpaper-blur").onchange=$("wallpaper-veil").onchange=saveWallSettings;
  const preview=$("wallpaper-preview");
  preview.addEventListener("dragover",event=>{event.preventDefault();preview.classList.add("dragging");});
  preview.addEventListener("dragleave",()=>preview.classList.remove("dragging"));
  preview.addEventListener("drop",event=>{event.preventDefault();preview.classList.remove("dragging");const file=event.dataTransfer?.files?.[0];if(file)setWallImage(file);});

  setInterval(tick,1000);setInterval(refresh,5000);refresh();
}
