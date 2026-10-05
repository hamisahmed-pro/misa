/* Misa AOT runtime — DOM bridge (Phase 4, M3).
 *
 * --js-library file implementing the JS half of rt/ui_dom.c.
 * Component -> DOM mapping (all inside #misa-root):
 *
 *   Column / Row -> <div style="display:flex;flex-direction:...">
 *   Spacer       -> <div style="flex:1 1 auto">
 *   Text         -> <p>                 Heading -> <h2>
 *   Banner       -> <div class="misa-banner misa-banner-{info,warning,error}">
 *   Button       -> <button> + click listener -> dispatch
 *   TextInput    -> <input type="text" placeholder="..."> (readback=.value)
 *   CameraButton -> <button> + hidden <input type="file" accept="image/*">
 *                   (capture="environment" on real mobile browsers);
 *                   chosen file -> staged into the Emscripten FS ->
 *                   Photo handle -> dispatch
 *   Image        -> <img> (object URL over the staged bytes)
 *   List         -> <ul> / <li> per Text item
 *
 * Element ids are small ints owned here; the C driver only passes
 * them back. Events re-enter wasm via Module.ccall into the exported
 * misa_dispatch_handler (and misa_dom_photo for captures).
 */
mergeInto(LibraryManager.library, {
  misa_dom_create: function(kind, parent) {
    kind = UTF8ToString(kind);
    var el;
    switch (kind) {
      case '__screen__':
        el = document.createElement('div');
        el.className = 'misa-screen';
        break;
      case 'Column':
        el = document.createElement('div');
        el.style.display = 'flex';
        el.style.flexDirection = 'column';
        el.style.gap = '8px';
        break;
      case 'Row':
        el = document.createElement('div');
        el.style.display = 'flex';
        el.style.flexDirection = 'row';
        el.style.gap = '8px';
        break;
      case 'Spacer':
        el = document.createElement('div');
        el.style.flex = '1 1 auto';
        break;
      case 'Text':
        el = document.createElement('p');
        break;
      case 'Heading':
        el = document.createElement('h2');
        break;
      case 'Banner':
        el = document.createElement('div');
        el.className = 'misa-banner misa-banner-info';
        break;
      case 'Button':
        el = document.createElement('button');
        el.type = 'button';
        break;
      case 'TextInput':
        el = document.createElement('input');
        el.type = 'text';
        break;
      case 'CameraButton': {
        el = document.createElement('span');
        el.className = 'misa-cambtn';
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = 'Take photo';
        var inp = document.createElement('input');
        inp.type = 'file';
        inp.accept = 'image/*';
        // real capture on mobile browsers; file picker on desktop
        inp.setAttribute('capture', 'environment');
        inp.style.display = 'none';
        btn.addEventListener('click', function() { inp.click(); });
        el.appendChild(btn);
        el.appendChild(inp);
        el._misaFileInput = inp;
        break;
      }
      case 'Image':
        el = document.createElement('img');
        el.className = 'misa-image';
        break;
      case 'List':
        el = document.createElement('ul');
        break;
      default:
        el = document.createElement('div');
        el.setAttribute('data-misa-kind', kind);
    }
    var id = ++MisaDom.next;
    MisaDom.els[id] = el;
    if (parent) {
      var p = MisaDom.els[parent | 0];
      if (p) p.appendChild(el);
    }
    return id;
  },

  misa_dom_set_text: function(dom, text) {
    var el = MisaDom.els[dom | 0];
    if (el) el.textContent = UTF8ToString(text);
  },

  misa_dom_set_banner_style: function(dom, style) {
    var el = MisaDom.els[dom | 0];
    if (el) el.className =
      'misa-banner misa-banner-' + UTF8ToString(style);
  },

  misa_dom_set_hint: function(dom, hint) {
    var el = MisaDom.els[dom | 0];
    if (el && 'placeholder' in el)
      el.placeholder = UTF8ToString(hint);
  },

  misa_dom_set_image: function(dom, path) {
    var el = MisaDom.els[dom | 0];
    if (!el) return;
    try {
      var bytes = FS.readFile(UTF8ToString(path));
      var url = URL.createObjectURL(new Blob([bytes]));
      el.src = url;
    } catch (e) {
      console.error('misa: Image: cannot read ' +
                    UTF8ToString(path) + ': ' + e);
    }
  },

  misa_dom_list_item: function(dom, text) {
    var el = MisaDom.els[dom | 0];
    if (!el) return;
    var li = document.createElement('li');
    li.textContent = UTF8ToString(text);
    el.appendChild(li);
  },

  misa_dom_set_handler: function(dom, event, handler_id) {
    var el = MisaDom.els[dom | 0];
    if (!el) return;
    event = UTF8ToString(event);
    handler_id = handler_id | 0;
    if (event === 'onTap') {
      // CameraButton's inner button only opens the file picker (wired
      // at creation); onTap is a no-op there.
      if (el._misaFileInput) return;
      el.addEventListener('click', function() {
        Module.ccall('misa_dispatch_handler', 'number',
                     ['number', 'number'], [handler_id, 0]);
      });
    } else if (event === 'onCapture' && el._misaFileInput) {
      var inp = el._misaFileInput;
      inp.addEventListener('change', function() {
        var file = inp.files && inp.files[0];
        if (!file) return;
        var reader = new FileReader();
        reader.onload = function() {
          try {
            FS.mkdir('/misa_uploads');
          } catch (e) { /* EEXIST is fine */ }
          var vpath = '/misa_uploads/' +
            String(Date.now()) + '_' +
            file.name.replace(/[^A-Za-z0-9._-]/g, '_');
          FS.writeFile(vpath, new Uint8Array(reader.result));
          var photo = Module.ccall('misa_dom_photo', 'number',
                                   ['string'], [vpath]);
          Module.ccall('misa_dispatch_handler', 'number',
                       ['number', 'number'], [handler_id, photo]);
        };
        reader.readAsArrayBuffer(file);
      });
    }
  },

  misa_dom_show: function(root) {
    var mount = document.getElementById('misa-root');
    if (!mount) return;
    while (mount.firstChild) mount.removeChild(mount.firstChild);
    var el = MisaDom.els[root | 0];
    if (el) mount.appendChild(el);
  },

  misa_dom_get_text: function(dom) {
    var el = MisaDom.els[dom | 0];
    var s = el ? String(el.value || '') : '';
    var len = lengthBytesUTF8(s) + 1;
    var p = _malloc(len);
    stringToUTF8(s, p, len);
    return p;
  },

  misa_dom_free: function(p) { _free(p); },

  misa_dom_report_error: function(text) {
    text = UTF8ToString(text);
    console.error(text);
    var mount = document.getElementById('misa-root');
    var banner = document.createElement('div');
    banner.className = 'misa-banner misa-banner-error misa-dismissible';
    banner.textContent = text;
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'misa-dismiss';
    close.textContent = '\u00d7';
    close.addEventListener('click', function() {
      if (banner.parentNode) banner.parentNode.removeChild(banner);
    });
    banner.appendChild(close);
    (mount || document.body).appendChild(banner);
  },
});
