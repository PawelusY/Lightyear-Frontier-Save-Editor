#!/usr/bin/env python3
# Lightyear Frontier Save Editor v0.2 - human-readable save inspector
import os, re, zlib, struct, hashlib, secrets, shutil, json, tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_TITLE = 'Lightyear Frontier Save Editor'
MAGIC = b'!hceMraF'
HEX_ID = rb'[A-Fa-f0-9]{16}'
PLAYER_KEY_RE = re.compile(rb'(' + HEX_ID + rb')\x00\x08\x00\x00\x00bIsHost')
MODULE_OWNER_RE = re.compile(rb'OwnerId\x00\x0c\x00\x00\x00StrProperty\x00.{0,24}?(' + HEX_ID + rb')', re.S)
ITEM_STRUCT = b'ItemStack_NoTemplate'

TOOL_LABELS = {
    'Hands':'Hands', 'GravityAxe':'Gravity Axe', 'RepairTool':'Repair Tool',
    'SeedBag':'Seed Bag', 'WateringCan':'Watering Can', 'MoundMaker':'Mound Maker',
    'PaintGun':'Paint Gun'
}
MODULE_LABELS = {
    'StandardEngine':'Standard Engine','StandardWindshield':'Standard Windshield',
    'StandardLeftArm':'Standard Left Arm','StandardRightArm':'Standard Right Arm',
    'StandardLeftLeg':'Standard Left Leg','StandardRightLeg':'Standard Right Leg',
    'Jackhammer0':'Jackhammer (Basic)','Jackhammer1':'Jackhammer (Upgraded)',
    'Harvester0':'Harvester','Plow0':'Plow','Vacuum0':'Vacuum','Hose0':'Hose',
    'SeedCannon0':'Seed Cannon','SeedCannon1':'Seed Cannon (Upgraded)',
    'SeedShooter0':'Seed Shooter'
}

def load_save(path):
    raw = open(path, 'rb').read()
    if raw.startswith(MAGIC) and len(raw) > 20:
        try:
            payload = zlib.decompress(raw[20:])
            return {'path':path,'raw':raw,'payload':payload,'kind':'wrapped','prefix':raw[:12]}
        except zlib.error:
            pass
    if raw.startswith(b'GVAS'):
        return {'path':path,'raw':raw,'payload':raw,'kind':'gvas'}
    raise ValueError('Unrecognized save format. The file has not been modified.')

def save_payload(info, payload, out_path):
    if info['kind'] == 'wrapped':
        compressed = zlib.compress(payload, 9)
        out = info['prefix'] + struct.pack('<I', len(payload)) + struct.pack('<I', len(compressed)) + compressed
    else:
        out = payload
    with open(out_path, 'wb') as f: f.write(out)

def ascii_strings(data, min_len=3):
    return [(m.start(), m.group().decode('ascii','replace')) for m in re.finditer(rb'[\x20-\x7e]{%d,}' % min_len, data)]

def player_map_bounds(data):
    p = data.find(b'PlayerData')
    if p < 0: return None, None
    marker = b'PlayerData\x00\x0c\x00\x00\x00MapProperty\x00'
    q = data.find(marker, p, min(len(data), p + 100))
    if q < 0: return p, min(len(data), p + 800000)
    size_pos = q + len(marker)
    if size_pos + 8 <= len(data):
        declared = struct.unpack_from('<Q', data, size_pos)[0]
        data_start = size_pos + 8
        if 0 < declared <= len(data) - data_start:
            return data_start, data_start + declared
    end = data.find(b'MechModuleInstances', p)
    return p, (end if end >= 0 else min(len(data), p + 800000))

def read_profile_ids(data):
    start, end = player_map_bounds(data)
    if start is None: return []
    found=[]
    for m in PLAYER_KEY_RE.finditer(data, start, end):
        ident=m.group(1).decode('ascii').upper()
        if not any(x['id']==ident for x in found): found.append({'id':ident,'offset':m.start(1)})
    return found

def field_bool(data, field):
    # Unreal property: field + BoolProperty + metadata, then byte value.
    marker = field.encode() + b'\x00\r\x00\x00\x00BoolProperty\x00'
    p=data.find(marker)
    if p < 0: return None
    # Name/type terminator followed by property size/flags metadata; the byte is after 8 bytes.
    q=p+len(marker)+8
    if q < len(data): return data[q] != 0
    return None

def parse_items(data):
    items=[]
    pos=0
    while True:
        s=data.find(ITEM_STRUCT,pos)
        if s<0: break
        # Each item stack is small; avoid reading into the next stack.
        e=data.find(ITEM_STRUCT,s+len(ITEM_STRUCT))
        if e<0 or e>s+1800: e=min(len(data),s+1800)
        chunk=data[s:e]
        # Read the value following Name / NameProperty from a length-prefixed Unreal string.
        name=None
        nm=chunk.find(b'Name\x00\r\x00\x00\x00NameProperty\x00')
        if nm>=0:
            # FString/name serialization includes 8 bytes of property metadata, then an int32 length.
            base=nm+len(b'Name\x00\r\x00\x00\x00NameProperty\x00')
            for off in range(base, min(base+28,len(chunk)-5)):
                n=struct.unpack_from('<i',chunk,off)[0]
                if 3 <= n <= 80 and off+4+n <= len(chunk):
                    rawname=chunk[off+4:off+4+n]
                    if rawname.endswith(b'\x00') and re.fullmatch(rb'[A-Za-z][A-Za-z0-9_]{1,78}\x00',rawname):
                        name=rawname[:-1].decode('ascii','replace'); break
        qty=None
        q=chunk.find(b'Size\x00\x0c\x00\x00\x00IntProperty\x00')
        if q>=0:
            v=q+len(b'Size\x00\x0c\x00\x00\x00IntProperty\x00')+9
            if v+4<=len(chunk):
                n=struct.unpack_from('<i',chunk,v)[0]
                if 0<=n<=1000000: qty=n
        if name and name not in ('None','ItemStack_NoTemplate'):
            items.append({'name':name,'qty':qty})
        pos=s+len(ITEM_STRUCT)
    # Keep first occurrence per item name+quantity, but allow duplicate stacks.
    return items

def extract_modules(data):
    result={}
    for m in MODULE_OWNER_RE.finditer(data):
        owner=m.group(1).decode('ascii').upper()
        chunk=data[m.end():m.end()+220]
        nm=re.search(rb'Name\x00\r\x00\x00\x00NameProperty\x00.{0,24}?([A-Za-z][A-Za-z0-9_]{2,50})\x00',chunk,re.S)
        if not nm:
            # fallback: look for known module names in the immediate record
            for name in MODULE_LABELS:
                if name.encode() in chunk: nm_name=name; break
            else: continue
        else: nm_name=nm.group(1).decode('ascii','replace')
        if nm_name in MODULE_LABELS or nm_name.startswith(('Standard','Jackhammer','Harvester','Plow','Vacuum','Hose','SeedCannon','SeedShooter','Transform','InventoryCapacity','Sprint','HaulingCapacity')):
            result.setdefault(owner,[]).append(nm_name)
    return result

def find_player_records(payload):
    entries=read_profile_ids(payload)
    modules=extract_modules(payload)
    _, map_end = player_map_bounds(payload)
    records=[]
    for i,e in enumerate(entries):
        start=e['offset']
        stop=entries[i+1]['offset'] if i+1<len(entries) else (map_end if map_end and map_end > start else min(len(payload),start+50000))
        # Bound each profile to its key and the next map key; the last record is capped to avoid world data.
        segment=payload[start:stop]
        items=parse_items(segment)
        tools=[]
        fp=segment.find(b'FarmerTools')
        if fp>=0:
            te=segment.find(b'EquippedFarmerTool',fp)
            if te<0: te=min(len(segment),fp+1200)
            sub=segment[fp:te]
            for _,s in ascii_strings(sub):
                if s in TOOL_LABELS and s not in tools: tools.append(s)
        intro=field_bool(segment,'bIsIntroState')
        host=field_bool(segment,'bIsHost')
        # A player record with no item stack records may still have an inventory stored elsewhere.
        record={'id':e['id'],'offset':start,'length':stop-start,'items':items,'tools':tools,
                'intro':intro,'host':host,'modules':modules.get(e['id'],[]),'segment':segment}
        records.append(record)
    return records

def pretty_item(name):
    return name.replace('_',' ').replace('Economics ','').replace('Crafting ','')

def build_report(info):
    data=info['payload']; records=find_player_records(data)
    lines=[f'FILE: {os.path.basename(info["path"])}',f'File size: {len(info["raw"]):,} B',f'Decompressed data size: {len(data):,} B',
           f'Format: {info["kind"]}', '', f'Player profiles detected: {len(records)}',
           'Note: this is a heuristic inspection. Fields that cannot be read reliably are marked as unknown.', '']
    for idx,r in enumerate(records,1):
        lines += [f'PLAYER {idx}',f'  ID: {r["id"]}',f'  Data offset: {r["offset"]:,}',
                  f'  Role: {"World host" if r["host"] else "Client / unknown" if r["host"] is None else "Client"}',
                  f'  Intro state: {"enabled" if r["intro"] else "disabled" if r["intro"] is False else "unknown"}',
                  f'  Recognized mech inventory entries: {len(r["items"])}']
        if r['items']:
            for it in r['items'][:40]:
                q=f' × {it["qty"]}' if it['qty'] is not None else ''
                lines.append(f'    • {pretty_item(it["name"])}{q}')
            if len(r['items'])>40: lines.append(f'    … oraz {len(r["items"])-40} kolejnych stosów')
        else: lines.append('    • Could not reliably read inventory stacks in this profile section.')
        if r['tools']:
            lines.append('  Player tools: ' + ', '.join(TOOL_LABELS.get(t,t) for t in r['tools']))
        if r['modules']:
            unique=[]
            for m in r['modules']:
                if m not in unique: unique.append(m)
            lines.append('  Parts/modules linked to this ID:')
            for m in unique: lines.append('    • '+MODULE_LABELS.get(m,m))
        else: lines.append('  Mech parts: no reliable OwnerId links found')
        lines.append('')
    if not records:
        lines += ['No PlayerData entries found in the expected format.', 'You can still inspect text fields in the Technical Data tab.']
    return '\n'.join(lines),records

class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title(APP_TITLE+' v0.5'); self.geometry('1120x760'); self.minsize(900,620)
        self.info=None; self.records=[]; self._build()
    def _build(self):
        top=ttk.Frame(self,padding=10); top.pack(fill='x')
        ttk.Button(top,text='Open .sav Save',command=self.open_file).pack(side='left')
        ttk.Button(top,text='Compare with Another Save',command=self.compare_file).pack(side='left',padx=8)
        ttk.Button(top,text='Export TXT Report',command=self.export_report).pack(side='left')
        ttk.Button(top,text='Undo Transfer / Restore Original',command=self.undo_transfer).pack(side='left',padx=8)
        self.path_label=ttk.Label(top,text='No file selected'); self.path_label.pack(side='left',padx=8,fill='x',expand=True)
        note=ttk.Label(self,text='Version 0.5: every ID remap creates a reversible original-save backup beside the output. Use Undo Transfer to restore the exact pre-transfer save.',wraplength=1080,foreground='#8a4b08')
        note.pack(fill='x',padx=12,pady=(0,8))
        panes=ttk.Panedwindow(self,orient='horizontal'); panes.pack(fill='both',expand=True,padx=10,pady=5)
        left=ttk.Frame(panes,padding=4); right=ttk.Frame(panes,padding=4); panes.add(left,weight=1); panes.add(right,weight=3)
        ttk.Label(left,text='Profiles Found in Save').pack(anchor='w')
        self.tree=ttk.Treeview(left,columns=('status','items','modules'),show='tree headings',height=22)
        self.tree.heading('#0',text='Player ID'); self.tree.heading('status',text='Role'); self.tree.heading('items',text='Stacks'); self.tree.heading('modules',text='Modules')
        self.tree.column('#0',width=175); self.tree.column('status',width=100); self.tree.column('items',width=55,anchor='center'); self.tree.column('modules',width=65,anchor='center')
        self.tree.pack(fill='both',expand=True,pady=5); self.tree.bind('<<TreeviewSelect>>',self.select_profile)
        self.tabs=ttk.Notebook(right); self.tabs.pack(fill='both',expand=True)
        tab1=ttk.Frame(self.tabs); tab2=ttk.Frame(self.tabs); self.tabs.add(tab1,text='Readable Report'); self.tabs.add(tab2,text='Technical Data')
        self.report=tk.Text(tab1,wrap='word',font=('Segoe UI',10)); self.report.pack(fill='both',expand=True)
        self.tech=tk.Text(tab2,wrap='none',font=('Consolas',9)); self.tech.pack(fill='both',expand=True)
        edit=ttk.LabelFrame(self,text='Remap Player ID (Advanced — not a profile transfer)',padding=9); edit.pack(fill='x',padx=10,pady=8)
        ttk.Label(edit,text='Old ID').grid(row=0,column=0,sticky='w'); self.old=ttk.Entry(edit,width=24); self.old.grid(row=0,column=1,padx=5)
        ttk.Label(edit,text='New ID').grid(row=0,column=2,sticky='w'); self.new=ttk.Entry(edit,width=24); self.new.grid(row=0,column=3,padx=5)
        ttk.Button(edit,text='Replace and Save As…',command=self.replace_id).grid(row=0,column=4,padx=8)
        ttk.Label(edit,text='Replaces all exact ID occurrences in ASCII/UTF-16LE data. This is not an automatic profile transfer.',foreground='#8a4b08').grid(row=1,column=0,columnspan=5,sticky='w',pady=(6,0))
        self.status=tk.StringVar(value='Ready. Open a save to view a player-friendly summary.')
        ttk.Label(self,textvariable=self.status,relief='sunken',anchor='w',padding=5).pack(fill='x',side='bottom')
    def set_text(self,w,text):
        w.configure(state='normal'); w.delete('1.0','end'); w.insert('end',text); w.configure(state='disabled')
    def open_file(self):
        path=filedialog.askopenfilename(title='Select Game Save',filetypes=[('Save files','*.sav'),('All files','*.*')])
        if not path:return
        try: self.info=load_save(path); report,self.records=build_report(self.info)
        except Exception as e: messagebox.showerror('Unable to Open Save',str(e)); return
        self.path_label.configure(text=path); self.set_text(self.report,report)
        strings=ascii_strings(self.info['payload'])
        self.set_text(self.tech,'\n'.join(f'{off:>10,}  {s}' for off,s in strings if any(k in s.lower() for k in ['player','inventory','mech','ownerid','itemstack','farmer','quest','intro','module']))[:100000])
        for x in self.tree.get_children(): self.tree.delete(x)
        for i,r in enumerate(self.records,1):
            typ='Host' if r['host'] else 'Client' if r['host'] is False else 'Unknown'
            self.tree.insert('','end',iid=r['id'],text=r['id'],values=(typ,len(r['items']),len(set(r['modules']))))
        self.status.set(f'Loaded {os.path.basename(path)} — detected {len(self.records)} profiles. The report does not modify the file.')
    def select_profile(self,_=None):
        sel=self.tree.selection()
        if not sel:return
        ident=sel[0]
        self.old.delete(0,'end'); self.old.insert(0,ident)
        for r in self.records:
            if r['id']==ident:
                lines=[f'Selected profile: {ident}','',f'Recognized inventory entries: {len(r["items"])}']
                for it in r['items'][:60]: lines.append(f'• {pretty_item(it["name"])}'+(f' × {it["qty"]}' if it['qty'] is not None else ''))
                lines += ['', 'Mech parts/modules:']
                lines += ['• '+MODULE_LABELS.get(m,m) for m in dict.fromkeys(r['modules'])] or ['• No reliable OwnerId link found.']
                lines += ['', 'Tools: '+(', '.join(TOOL_LABELS.get(t,t) for t in r['tools']) if r['tools'] else 'unknown')]
                self.set_text(self.report,'\n'.join(lines)); break
    def compare_file(self):
        if not self.info: messagebox.showinfo('Open a Save First','Open the first file, then select the second.'); return
        path=filedialog.askopenfilename(title='Select the Second Save to Compare',filetypes=[('Save files','*.sav'),('All files','*.*')])
        if not path:return
        try:
            other=load_save(path); rep_a,ra=build_report(self.info); rep_b,rb=build_report(other)
        except Exception as e: messagebox.showerror('Comparison Error',str(e)); return
        def sig(r): return {x['id']:{'items':sorted((it['name'],it['qty']) for it in x['items']),'modules':sorted(set(x['modules'])),'tools':sorted(x['tools']),'intro':x['intro']} for x in r}
        a,b=sig(ra),sig(rb); lines=['SAVE COMPARISON',f'A: {self.info["path"]}',f'B: {path}','']
        lines.append(f'Profiles in A: {len(a)} | profiles in B: {len(b)}')
        for ident in sorted(set(a)|set(b)):
            if ident not in a: lines += ['',f'{ident}: only in the second save']; continue
            if ident not in b: lines += ['',f'{ident}: only in the first save']; continue
            aa,bb=a[ident],b[ident]
            if aa==bb: continue
            lines += ['',f'Player {ident}:']
            for key,label in [('items','Inventory'),('modules','Mech modules'),('tools','Tools'),('intro','Intro state')]:
                if aa[key]!=bb[key]: lines.append(f'  {label}: A={aa[key]} | B={bb[key]}')
        if len(lines)==5: lines.append('No differences found in the recognized profile fields.')
        self.set_text(self.report,'\n'.join(lines)); self.tabs.select(0); self.status.set('Comparison complete. No files were modified.')
    def export_report(self):
        if not self.info: messagebox.showinfo('No Save Loaded','Open a .sav file first.'); return
        report,_=build_report(self.info); out=filedialog.asksaveasfilename(title='Save Report',initialfile=os.path.splitext(os.path.basename(self.info['path']))[0]+'_report.txt',defaultextension='.txt',filetypes=[('Text report','*.txt')])
        if out:
            with open(out,'w',encoding='utf-8') as f:f.write(report)
            self.status.set('Report exported: '+out)
    def undo_transfer(self):
        if not self.info:
            messagebox.showinfo('Open a Save First','Open the transferred .sav file first, then choose Undo Transfer / Restore Original.')
            return
        current_path = self.info['path']
        backup_path = current_path + '.undo.sav'
        manifest_path = current_path + '.undo.json'
        if not os.path.isfile(backup_path) or not os.path.isfile(manifest_path):
            messagebox.showwarning(
                'Undo Backup Not Found',
                'No undo backup was found for this save. Version 0.5 creates files named:\n'
                '  <save>.sav.undo.sav\n  <save>.sav.undo.json\n\n'
                'Keep both files beside the transferred save. Older saves made with v0.5 cannot be reliably reversed automatically.'
            )
            return
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            with open(current_path, 'rb') as f:
                current_bytes = f.read()
            with open(backup_path, 'rb') as f:
                original_bytes = f.read()
            actual_hash = hashlib.sha256(current_bytes).hexdigest()
            if actual_hash != manifest.get('output_sha256'):
                if not messagebox.askyesno(
                    'Save Has Changed',
                    'This save does not match the exact file created by the transfer (it may have been saved by the game).\n\n'
                    'Restore the original save anyway? This will undo the transfer and also discard changes made after the transfer.'
                ):
                    return
            if hashlib.sha256(original_bytes).hexdigest() != manifest.get('original_sha256'):
                messagebox.showerror('Backup Verification Failed','The original-save backup does not match its verification hash. Nothing was changed.')
                return
        except Exception as e:
            messagebox.showerror('Undo Error',f'Could not verify undo backup: {e}')
            return
        suggested = os.path.splitext(os.path.basename(current_path))[0] + '_RESTORED_ORIGINAL.sav'
        out = filedialog.asksaveasfilename(title='Save Restored Original Save', initialfile=suggested,
            defaultextension='.sav', filetypes=[('Save files','*.sav')])
        if not out:
            return
        try:
            # Copy the exact original bytes, not a reverse text replacement.
            with open(out, 'wb') as f:
                f.write(original_bytes)
            verify = load_save(out)
            if hashlib.sha256(verify['raw']).hexdigest() != manifest.get('original_sha256'):
                raise ValueError('Restored file did not pass byte-for-byte verification.')
        except Exception as e:
            messagebox.showerror('Restore Error',f'Could not restore original save: {e}')
            return
        self.status.set(f'Restored exact pre-transfer save to {out}')
        messagebox.showinfo('Original Save Restored',
            'The original save from before the transfer has been restored byte-for-byte to a NEW file.\n\n'
            'Use that restored file in the game. The transferred save and its undo backup were left unchanged.')

    def replace_id(self):
        if not self.info: messagebox.showinfo('No File Loaded','Open a save first.'); return
        old=self.old.get().strip().upper(); new=self.new.get().strip().upper()
        if not re.fullmatch(r'[A-F0-9]{16}',old) or not re.fullmatch(r'[A-F0-9]{16}',new): messagebox.showerror('Invalid ID','Both IDs must be exactly 16 hexadecimal characters.'); return
        if old==new: messagebox.showinfo('No Changes','The IDs are identical.'); return
        data=self.info['payload']
        existing={r['id'] for r in find_player_records(data)}
        displaced_id=None
        if new in existing:
            # Preserve the existing destination profile by moving its ID/references
            # to a generated unused ID first. Then move the selected source profile
            # onto the requested destination ID. This avoids a map-key collision.
            while True:
                candidate=secrets.token_hex(8).upper()
                if candidate not in existing and candidate not in (old,new):
                    displaced_id=candidate
                    break
            proceed=messagebox.askyesno(
                'Destination ID Already Exists',
                f'The destination ID {new} already has a profile in this save.\n\n'
                f'If you continue, the existing destination profile and its ID references will be preserved under a newly generated ID:\n{displaced_id}\n\n'
                f'Then {old} will be remapped to {new}. This is an ID remap, not a verified full profile transfer, and the displaced profile may no longer be associated with a normal player login.\n\n'
                'Continue and save to a NEW file?'
            )
            if not proceed: return
        ca=data.count(old.encode()); cu=data.count(old.encode().decode().encode('utf-16le'))
        if ca+cu==0: messagebox.showwarning('ID Not Found','The exact ID was not found.'); return
        prompt=(f'Remap {old} → {new}?\nFound: {ca} ASCII occurrences and {cu} UTF-16LE occurrences.')
        if displaced_id:
            prompt += f'\n\nExisting destination profile {new} will first be moved to {displaced_id} so it is not overwritten.'
        prompt += '\n\nThis changes IDs/references; it does not verify or clone every profile field. A new file will be saved; the source file remains unchanged.'
        if not messagebox.askyesno('Confirm ID Remap',prompt): return
        edited=data
        if displaced_id:
            edited=edited.replace(new.encode(),displaced_id.encode()).replace(new.encode().decode().encode('utf-16le'),displaced_id.encode().decode().encode('utf-16le'))
        edited=edited.replace(old.encode(),new.encode()).replace(old.encode().decode().encode('utf-16le'),new.encode().decode().encode('utf-16le'))
        suggested=os.path.splitext(os.path.basename(self.info['path']))[0]+'_edited.sav'
        out=filedialog.asksaveasfilename(title='Save Modified Save',initialfile=suggested,defaultextension='.sav',filetypes=[('Save files','*.sav')])
        if not out:return
        try:
            save_payload(self.info,edited,out); check=load_save(out)
            if len(check['payload'])!=len(edited): raise ValueError('Length validation failed.')
            # Store the exact original file bytes, not just the decompressed payload.
            # Undo restores this file rather than guessing how to reverse random IDs.
            backup_path = out + '.undo.sav'
            manifest_path = out + '.undo.json'
            if os.path.exists(backup_path) or os.path.exists(manifest_path):
                raise FileExistsError('An undo backup already exists for this output name. Choose a different output filename.')
            with open(backup_path, 'wb') as f:
                f.write(self.info['raw'])
            with open(out, 'rb') as f:
                output_hash = hashlib.sha256(f.read()).hexdigest()
            manifest = {
                'version': 1,
                'output_file': os.path.basename(out),
                'output_sha256': output_hash,
                'original_file': os.path.basename(self.info['path']),
                'original_sha256': hashlib.sha256(self.info['raw']).hexdigest(),
                'source_id': old,
                'destination_id': new,
                'displaced_id': displaced_id,
                'backup_file': os.path.basename(backup_path),
                'warning': 'Undo restores the exact pre-transfer save; changes made in-game after the transfer are not included.'
            }
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(manifest, f, indent=2)
        except Exception as e:
            messagebox.showerror('Save Error',str(e)); return
        self.status.set(f'Saved {out}; changed {ca+cu} source-ID occurrences. Reversible backup created.')
        messagebox.showinfo('Saved with Undo Backup',
            'Saved a new file and created its undo backup. Keep these THREE files together:\n\n'
            f'1. {os.path.basename(out)}\n2. {os.path.basename(backup_path)}\n3. {os.path.basename(manifest_path)}\n\n'
            'To undo: open the transferred save in this editor and click “Undo Transfer / Restore Original”. '
            'It restores the exact original save, avoiding random/new IDs.\n\n'
            + (f'The previous destination ID was moved to {displaced_id}. ' if displaced_id else '')
            + 'Reminder: ID remapping is not a verified full profile clone. Test on a copy.')

if __name__=='__main__': App().mainloop()
