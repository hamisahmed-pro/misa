/* Misa AOT runtime — UIKit UI driver (Phase 4, M7).
 *
 * Implements rt/misa_ui.h on iOS. Widget handles are 1-based indices
 * into a retained NSMutableArray of UIView* (index 0 = NSNull
 * placeholder; handle 0 = none). A parallel C array keeps each
 * widget's kind string, id: binding slot, and handler id.
 *
 * Component mapping (spec/PHASE4.md §4.4, docs/ios-cross.md §6.1):
 *   Column       -> UIStackView (vertical)
 *   Row          -> UIStackView (horizontal)
 *   Spacer       -> UIView, low hugging priority (flexible in stacks)
 *   Text         -> UILabel, numberOfLines = 0
 *   Heading      -> UILabel, bold headline font
 *   Banner       -> UILabel tinted by style (info/warning/error)
 *   Button       -> UIButton (system) + target-action ->
 *                   misa_dispatch_handler
 *   TextInput    -> UITextField (+ get_text readback)
 *   CameraButton -> UIButton + UIImagePickerController; the captured
 *                   JPEG lands in Documents and its path becomes the
 *                   Photo handle for the onCapture handler
 *   Image        -> UIImageView (ScaleAspectFit)
 *   List         -> UITableView, single text row type
 *
 * Main-thread discipline: UIKit is main-thread only. The generated
 * call stream is synchronous, so mutating entry points run through
 * ui_main_sync() — inline when already on main, dispatch_sync
 * otherwise. Model calls run on worker threads and block on a
 * semaphore whose completion fires on an NSURLSession background
 * queue, so dispatch_sync to main never deadlocks.
 *
 * Handler-boundary errors go to misa_ui_report_error (dismissible
 * alert) — the app is never killed, mirroring ui_android.c.
 */
#import "misa_ui.h"

#import "misa_platform.h"

#import <UIKit/UIKit.h>
#import <objc/runtime.h>
#import <os/log.h>

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* driver state                                                       */
/* ------------------------------------------------------------------ */

/* ARC forbids __strong object pointers in structs, so the ObjC
 * members are __unsafe_unretained and the arrays are explicitly
 * CFRetained/CFReleased. Views are additionally retained by their
 * superviews / the views array. */
typedef struct {
    char    kind[32];    /* component kind ("TextInput", ...) */
    int32_t bound_slot;  /* id: registry slot, -1 = unbound */
    int32_t handler_id;  /* Button.onTap / CameraButton.onCapture, -1 */
} UIKitWidgetMeta;

struct MisaUI {
    __unsafe_unretained NSMutableArray *views; /* 1-based UIView*/;
    UIKitWidgetMeta *meta;      /* parallel, 1-based */
    int32_t          nw, wcap;  /* nw = highest handle in use */
    __unsafe_unretained UIView *rootView; /* set by misa_ui_set_root_view */
    /* shim call-stack (misa_screen_* layer) */
    MisaWidget *stack;
    int32_t     ssp, scap;
};

/* Process-global driver instance (selected at link time; generated
 * code reaches it via misa_ui_active()). */
static MisaUI *g_ui = NULL;

/* Dispatches may arrive from UI callbacks (main) or worker threads. */
static pthread_mutex_t g_dispatch_mu = PTHREAD_MUTEX_INITIALIZER;

/* Camera capture filenames. */
static int g_capture_seq = 0;

static void ui_main_sync(void (^block)(void)) {
    if ([NSThread isMainThread]) {
        block();
    } else {
        dispatch_sync(dispatch_get_main_queue(), block);
    }
}

static void ui_grow_meta(MisaUI *ui) {
    if (ui->nw < ui->wcap) return;
    int32_t ncap = ui->wcap ? ui->wcap * 2 : 16;
    UIKitWidgetMeta *nm = (UIKitWidgetMeta *)realloc(
        ui->meta, sizeof(UIKitWidgetMeta) * (size_t)ncap);
    if (!nm) misa_platform_fatal("E300", "out of memory");
    ui->meta = nm;
    ui->wcap = ncap;
}

static UIView *ui_view(MisaUI *ui, MisaWidget w) {
    if (w <= 0 || w > ui->nw) return nil;
    id o = [ui->views objectAtIndex:(NSUInteger)w];
    return (o == [NSNull null]) ? nil : (UIView *)o;
}

static UIKitWidgetMeta *ui_meta(MisaUI *ui, MisaWidget w) {
    if (w <= 0 || w > ui->nw) return NULL;
    return &ui->meta[w];
}

/* Host view controller for presenting pickers/alerts. */
static UIViewController *ui_host_vc(MisaUI *ui) {
    UIViewController *vc = ui->rootView.window.rootViewController;
    if (!vc) vc = [UIApplication sharedApplication].keyWindow.rootViewController;
    return vc;
}

MisaUI *misa_ui_create(void) {
    MisaUI *ui = (MisaUI *)calloc(1, sizeof(MisaUI));
    if (!ui) misa_platform_fatal("E300", "out of memory");
    ui->views = [[NSMutableArray alloc] initWithObjects:[NSNull null], nil];
    ui->nw = 0;
    ui->wcap = 0;
    return ui;
}

void misa_ui_destroy(MisaUI *ui) {
    if (!ui) return;
    CFRelease((CFTypeRef)ui->views);
    free(ui->meta);
    free(ui->stack);
    free(ui);
}

/* Phase 4 (M4): the driver instance for generated code. */
MisaUI *misa_ui_active(void) {
    if (!g_ui) g_ui = misa_ui_create();
    return g_ui;
}

/* main.m entry helper: the container misa_ui_show_screen attaches
 * screens to. */
void misa_ui_set_root_view(MisaUI *ui, void *rootView) {
    ui->rootView = (__bridge UIView *)rootView;
}

/* ------------------------------------------------------------------ */
/* tap trampoline + camera delegate                                   */
/* ------------------------------------------------------------------ */

@interface MisaTapTarget : NSObject
@property (nonatomic, copy) void (^action)(void);
- (void)tapped:(id)sender;
@end

@implementation MisaTapTarget
- (void)tapped:(id)sender {
    (void)sender;
    if (self.action) self.action();
}
@end

@interface MisaCameraDelegate : NSObject
    <UIImagePickerControllerDelegate, UINavigationControllerDelegate>
@property (nonatomic, assign) MisaUI *ui;
@property (nonatomic, assign) int32_t handlerId;
@end

@implementation MisaCameraDelegate

/* Capture -> JPEG in Documents -> Photo handle -> onCapture handler.
 * Cancel -> no event (mirrors the Android driver). */
- (void)imagePickerController:(UIImagePickerController *)picker
        didFinishPickingMediaWithInfo:(NSDictionary<NSString *, id> *)info {
    UIImage *img = info[UIImagePickerControllerOriginalImage];
    MisaUI *ui = self.ui;
    int32_t hid = self.handlerId;
    (void)ui;
    [picker dismissViewControllerAnimated:YES completion:^{
        if (!img) {
            misa_dispatch_handler(hid, NULL);
            return;
        }
        NSData *jpeg = UIImageJPEGRepresentation(img, 0.85);
        /* NSStrings: the block retains them; a stack C array would
         * be unsafe here (completion may run after we return). */
        NSString *rel = [NSString stringWithFormat:@"misa_capture_%d.jpg",
            __sync_add_and_fetch(&g_capture_seq, 1)];
        NSString *mapped = nil;
        char cpath[1024];
        if (jpeg && misa_platform_fs_path([rel UTF8String],
                                          cpath, sizeof cpath)) {
            mapped = [NSString stringWithUTF8String:cpath];
            if (![jpeg writeToFile:mapped atomically:YES])
                mapped = nil;
        }
        misa_dispatch_handler(hid,
            misa_media(0 /* Photo */, mapped ? [mapped UTF8String] : ""));
    }];
}

- (void)imagePickerControllerDidCancel:(UIImagePickerController *)picker {
    [picker dismissViewControllerAnimated:YES completion:nil];
}

@end

static void misa_camera_present(MisaUI *ui, MisaWidget w) {
    UIKitWidgetMeta *m = ui_meta(ui, w);
    int32_t hid = m ? m->handler_id : -1;
    if (hid < 0) return;
    if (![UIImagePickerController
            isSourceTypeAvailable:UIImagePickerControllerSourceTypeCamera]) {
        misa_raise("E201", "camera not available on this device", 0, 0);
        misa_ui_report_error(ui, misa_take_pending());
        return;
    }
    UIViewController *vc = ui_host_vc(ui);
    if (!vc) {
        misa_raise("E201", "no view controller to present camera from",
                   0, 0);
        misa_ui_report_error(ui, misa_take_pending());
        return;
    }
    MisaCameraDelegate *del = [MisaCameraDelegate new];
    del.ui = ui;
    del.handlerId = hid;
    UIImagePickerController *picker = [UIImagePickerController new];
    picker.sourceType = UIImagePickerControllerSourceTypeCamera;
    picker.delegate = del;
    /* The delegate must survive the presentation. */
    objc_setAssociatedObject(picker, @selector(delegate),
                             del, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    [vc presentViewController:picker animated:YES completion:nil];
}

/* ------------------------------------------------------------------ */
/* List data source                                                   */
/* ------------------------------------------------------------------ */

@interface MisaListData : NSObject <UITableViewDataSource>
@property (nonatomic, strong) NSArray<NSString *> *items;
@end

@implementation MisaListData
- (NSInteger)tableView:(UITableView *)t
        numberOfRowsInSection:(NSInteger)s {
    (void)t; (void)s;
    return (NSInteger)self.items.count;
}
- (UITableViewCell *)tableView:(UITableView *)t
        cellForRowAtIndexPath:(NSIndexPath *)p {
    static NSString *rid = @"misa";
    UITableViewCell *c = [t dequeueReusableCellWithIdentifier:rid];
    if (!c)
        c = [[UITableViewCell alloc]
                initWithStyle:UITableViewCellStyleDefault
                reuseIdentifier:rid];
    c.textLabel.text = self.items[(NSUInteger)p.row];
    return c;
}
@end

/* ------------------------------------------------------------------ */
/* tree construction                                                  */
/* ------------------------------------------------------------------ */

MisaWidget misa_ui_begin_screen(MisaUI *ui, const char *name) {
    (void)name;
    /* Fresh widget namespace per screen. */
    [ui->views removeAllObjects];
    [ui->views addObject:[NSNull null]];
    ui->nw = 0;
    return misa_ui_begin_comp(ui, 0, "__screen__");
}

MisaWidget misa_ui_begin_comp(MisaUI *ui, MisaWidget parent,
                              const char *kind) {
    __block MisaWidget w = 0;
    ui_main_sync(^{
        ui_grow_meta(ui);
        UIView *v = nil;
        if (strcmp(kind, "Column") == 0 || strcmp(kind, "Row") == 0) {
            UIStackView *s = [UIStackView new];
            s.axis = strcmp(kind, "Column") == 0
                ? UILayoutConstraintAxisVertical
                : UILayoutConstraintAxisHorizontal;
            v = s;
        } else if (strcmp(kind, "Text") == 0 ||
                   strcmp(kind, "Heading") == 0 ||
                   strcmp(kind, "Banner") == 0) {
            UILabel *l = [UILabel new];
            l.numberOfLines = 0;
            if (strcmp(kind, "Heading") == 0) {
                UIFont *f = [UIFont preferredFontForTextStyle:
                                        UIFontTextStyleHeadline];
                UIFontDescriptor *d = [f.fontDescriptor
                    fontDescriptorWithSymbolicTraits:
                        UIFontDescriptorTraitBold];
                if (d) l.font = [UIFont fontWithDescriptor:d size:0];
            } else if (strcmp(kind, "Banner") == 0) {
                l.layer.cornerRadius = 8;
                l.layer.masksToBounds = YES;
            }
            v = l;
        } else if (strcmp(kind, "Button") == 0 ||
                   strcmp(kind, "CameraButton") == 0) {
            v = [UIButton buttonWithType:UIButtonTypeSystem];
        } else if (strcmp(kind, "TextInput") == 0) {
            UITextField *t = [UITextField new];
            t.borderStyle = UITextBorderStyleRoundedRect;
            v = t;
        } else if (strcmp(kind, "Image") == 0) {
            UIImageView *iv = [UIImageView new];
            iv.contentMode = UIViewContentModeScaleAspectFit;
            v = iv;
        } else if (strcmp(kind, "List") == 0) {
            v = [[UITableView alloc] initWithFrame:CGRectZero
                                             style:UITableViewStylePlain];
        } else if (strcmp(kind, "Spacer") == 0) {
            UIView *s = [UIView new];
            [s setContentHuggingPriority:UILayoutPriorityDefaultLow
                                 forAxis:UILayoutConstraintAxisVertical];
            [s setContentHuggingPriority:UILayoutPriorityDefaultLow
                                 forAxis:UILayoutConstraintAxisHorizontal];
            v = s;
        } else if (strcmp(kind, "__screen__") == 0) {
            /* Screen root: a plain container UIView that
             * misa_ui_show_screen attaches full-bleed. */
            v = [UIView new];
        }
        ui->nw++;
        w = ui->nw;
        [ui->views addObject:v ? v : [NSNull null]];
        UIKitWidgetMeta *m = &ui->meta[w];
        strncpy(m->kind, kind ? kind : "", sizeof m->kind - 1);
        m->kind[sizeof m->kind - 1] = '\0';
        m->bound_slot = -1;
        m->handler_id = -1;
        UIView *p = ui_view(ui, parent);
        if (p && v) {
            if ([p isKindOfClass:[UIStackView class]])
                [(UIStackView *)p addArrangedSubview:v];
            else
                [p addSubview:v];
        }
    });
    return w;
}

void misa_ui_set_arg(MisaUI *ui, MisaWidget w,
                     const char *arg, MVal *v) {
    if (strcmp(arg, "items") == 0 && misa_tag(v) == M_TAG_LIST) {
        /* List(items:): one text row per item, rendered with the
         * existing misa_render (no new formatting rules). */
        NSMutableArray<NSString *> *rows = [NSMutableArray array];
        MVal *it = misa_iter_new(v, 0, 0);
        if (misa_pending()) return; /* cannot happen: tag checked */
        for (;;) {
            MVal *x = misa_iter_next(it);
            if (!x) break;
            MVal *rt = misa_render(x);
            [rows addObject:[NSString stringWithUTF8String:misa_text_z(rt)]];
        }
        ui_main_sync(^{
            UIView *vv = ui_view(ui, w);
            if (![vv isKindOfClass:[UITableView class]]) return;
            UITableView *t = (UITableView *)vv;
            MisaListData *data = [MisaListData new];
            data.items = rows;
            objc_setAssociatedObject(t, @selector(reloadData),
                                     data, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
            t.dataSource = data;
            [t reloadData];
        });
        return;
    }
    if (strcmp(arg, "photo") == 0) {
        if (misa_media_kind(v) < 0) return;
        const char *path = misa_media_path(v);
        ui_main_sync(^{
            UIView *vv = ui_view(ui, w);
            if ([vv isKindOfClass:[UIImageView class]] && path)
                ((UIImageView *)vv).image = [UIImage
                    imageWithContentsOfFile:
                        [NSString stringWithUTF8String:path]];
        });
        return;
    }
    MVal *t = misa_render(v);
    const char *z = misa_text_z(t);
    if (strcmp(arg, "text") == 0 || strcmp(arg, "label") == 0) {
        ui_main_sync(^{
            UIView *vv = ui_view(ui, w);
            if ([vv isKindOfClass:[UILabel class]])
                ((UILabel *)vv).text =
                    [NSString stringWithUTF8String:z];
            else if ([vv isKindOfClass:[UIButton class]])
                [(UIButton *)vv setTitle:[NSString stringWithUTF8String:z]
                                forState:UIControlStateNormal];
        });
    } else if (strcmp(arg, "hint") == 0) {
        ui_main_sync(^{
            UIView *vv = ui_view(ui, w);
            if ([vv isKindOfClass:[UITextField class]])
                ((UITextField *)vv).placeholder =
                    [NSString stringWithUTF8String:z];
        });
    } else if (strcmp(arg, "style") == 0) {
        /* Banner(text:, style:) tint. */
        ui_main_sync(^{
            UIView *vv = ui_view(ui, w);
            if (![vv isKindOfClass:[UILabel class]]) return;
            UIColor *c = nil;
            if (strcmp(z, "info") == 0) c = [UIColor systemBlueColor];
            else if (strcmp(z, "warning") == 0)
                c = [UIColor systemYellowColor];
            else if (strcmp(z, "error") == 0) c = [UIColor systemRedColor];
            if (c) ((UILabel *)vv).backgroundColor =
                        [c colorWithAlphaComponent:0.15f];
        });
    }
    /* unknown args are ignored (checker constrains the set) */
}

void misa_ui_set_handler(MisaUI *ui, MisaWidget w,
                         const char *event, int32_t handler_id) {
    UIKitWidgetMeta *m = ui_meta(ui, w);
    if (!m) return;
    m->handler_id = handler_id;
    int isTap = strcmp(event, "onTap") == 0;
    int isCapture = strcmp(event, "onCapture") == 0;
    if (!isTap && !isCapture) return;
    ui_main_sync(^{
        UIView *vv = ui_view(ui, w);
        if (![vv isKindOfClass:[UIButton class]]) return;
        UIButton *b = (UIButton *)vv;
        MisaTapTarget *target = [MisaTapTarget new];
        if (isTap) {
            target.action = ^{ misa_dispatch_handler(handler_id, NULL); };
        } else {
            /* CameraButton.onCapture: present the picker; the
             * delegate dispatches the Photo handle. */
            target.action = ^{ misa_camera_present(ui, w); };
        }
        objc_setAssociatedObject(b, @selector(tapped:),
                                 target, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
        [b addTarget:target action:@selector(tapped:)
            forControlEvents:UIControlEventTouchUpInside];
    });
}

void misa_ui_end_comp(MisaUI *ui, MisaWidget w) {
    (void)ui; (void)w;
    /* the tree is assembled as it is built */
}

void misa_ui_bind_id(MisaUI *ui, MisaWidget w, int32_t slot) {
    UIKitWidgetMeta *m = ui_meta(ui, w);
    if (m) m->bound_slot = slot;
}

void misa_ui_show_screen(MisaUI *ui, MisaWidget root) {
    os_log_debug(OS_LOG_DEFAULT,
                 "misa: show_screen ui=%p rootView=%p",
                 (void *)ui, (__bridge void *)ui->rootView);
    ui_main_sync(^{
        UIView *rv = ui_view(ui, root);
        UIView *container = ui->rootView;
        if (!rv || !container) return;
        for (UIView *s in [container.subviews copy])
            [s removeFromSuperview];
        rv.translatesAutoresizingMaskIntoConstraints = NO;
        [container addSubview:rv];
        [NSLayoutConstraint activateConstraints:@[
            [rv.topAnchor constraintEqualToAnchor:
                container.safeAreaLayoutGuide.topAnchor],
            [rv.leadingAnchor constraintEqualToAnchor:
                container.leadingAnchor],
            [rv.trailingAnchor constraintEqualToAnchor:
                container.trailingAnchor],
            [rv.bottomAnchor constraintEqualToAnchor:
                container.safeAreaLayoutGuide.bottomAnchor],
        ]];
    });
}

/* ------------------------------------------------------------------ */
/* readback + events                                                  */
/* ------------------------------------------------------------------ */

MisaWidget misa_ui_find_bound(MisaUI *ui, int32_t slot) {
    for (int32_t i = 1; i <= ui->nw; i++) {
        if (ui->meta[i].bound_slot == slot)
            return i;
    }
    return 0;
}

MVal *misa_ui_get_text(MisaUI *ui, MisaWidget w) {
    UIKitWidgetMeta *m = ui_meta(ui, w);
    if (!m || strcmp(m->kind, "TextInput") != 0) {
        misa_raise("E201", "cannot read '.text': no TextInput is "
                   "bound to this id on the current screen", 0, 0);
        return NULL;
    }
    __block MVal *t = NULL;
    ui_main_sync(^{
        UIView *vv = ui_view(ui, w);
        NSString *s = [vv isKindOfClass:[UITextField class]]
            ? ((UITextField *)vv).text : nil;
        t = misa_text_cstr(s ? [s UTF8String] : "");
    });
    return t;
}

/* Phase 4 (M4): dispatch a compiled event handler through the
 * generated misa_handler_table[]. Returns 0 when the handler ran
 * clean, 1 when it raised (reported via misa_ui_report_error — the
 * app is never killed), -1 for an unknown handler id. */
int32_t misa_dispatch_handler(int32_t handler_id, MVal *event) {
    int32_t rc;
    pthread_mutex_lock(&g_dispatch_mu);
    if (handler_id < 0 || handler_id >= misa_num_handlers) {
        rc = -1;
    } else {
        MVal *r = misa_handler_table[handler_id](event);
        (void)r; /* handler return values are ignored */
        if (misa_pending()) {
            MVal *err = misa_take_pending();
            misa_ui_report_error(misa_ui_active(), err);
            rc = 1;
        } else {
            rc = 0;
        }
    }
    pthread_mutex_unlock(&g_dispatch_mu);
    return rc;
}

/* Handler-boundary errors: dismissible alert, never killing the app. */
void misa_ui_report_error(MisaUI *ui, MVal *err) {
    MVal *rec = misa_catch_record(err);
    const char *code = "?";
    const char *msg = "?";
    if (rec) {
        MVal *c = misa_record_get(rec, "code", 0, 0);
        MVal *mm = misa_record_get(rec, "message", 0, 0);
        if (c && !misa_pending()) code = misa_text_z(c);
        if (mm && !misa_pending()) msg = misa_text_z(mm);
    }
    NSString *title = [NSString stringWithFormat:@"Error %s", code];
    NSString *body = [NSString stringWithUTF8String:msg];
    ui_main_sync(^{
        UIViewController *vc = ui_host_vc(ui);
        if (!vc) return;
        UIAlertController *a = [UIAlertController
            alertControllerWithTitle:title message:body
            preferredStyle:UIAlertControllerStyleAlert];
        [a addAction:[UIAlertAction actionWithTitle:@"Dismiss"
                     style:UIAlertActionStyleDefault handler:nil]];
        [vc presentViewController:a animated:YES completion:nil];
    });
}

/* ------------------------------------------------------------------ */
/* misa_screen_* compatibility shims (same shape as ui_android.c)      */
/* ------------------------------------------------------------------ */

static void shim_push(MisaWidget w) {
    if (!g_ui) return;
    if (g_ui->ssp >= g_ui->scap) {
        int32_t ncap = g_ui->scap ? g_ui->scap * 2 : 32;
        MisaWidget *ns = (MisaWidget *)realloc(g_ui->stack,
                                              sizeof(MisaWidget) * (size_t)ncap);
        if (!ns) misa_platform_fatal("E300", "out of memory");
        g_ui->stack = ns;
        g_ui->scap = ncap;
    }
    g_ui->stack[g_ui->ssp++] = w;
}

void misa_screen_begin(const char *name) {
    if (!g_ui) g_ui = misa_ui_create();
    MisaWidget root = misa_ui_begin_screen(g_ui, name);
    g_ui->ssp = 0;
    shim_push(root);
}

void misa_screen_comp_begin(const char *name, int has_args) {
    (void)has_args;
    MisaWidget parent = g_ui->stack[g_ui->ssp - 1];
    MisaWidget w = misa_ui_begin_comp(g_ui, parent, name);
    shim_push(w);
}

void misa_screen_comp_arg(const char *name, MVal *v, int is_first) {
    (void)is_first;
    misa_ui_set_arg(g_ui, g_ui->stack[g_ui->ssp - 1], name, v);
}

void misa_screen_comp_arg_handler(const char *name, int is_first,
                                  int handler_id) {
    (void)is_first;
    misa_ui_set_handler(g_ui, g_ui->stack[g_ui->ssp - 1], name,
                        handler_id);
}

void misa_screen_comp_bind_id(int32_t slot) {
    if (!g_ui || g_ui->ssp <= 0) return;
    misa_ui_bind_id(g_ui, g_ui->stack[g_ui->ssp - 1], slot);
}

void misa_screen_comp_head_end(int has_args) {
    (void)has_args;
}

void misa_screen_comp_end(void) {
    MisaWidget w = g_ui->stack[--g_ui->ssp];
    misa_ui_end_comp(g_ui, w);
}

void misa_screen_end(void) {
    misa_ui_show_screen(g_ui, g_ui->stack[0]);
    g_ui->ssp = 0;
}
