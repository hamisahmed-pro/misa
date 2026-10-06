/* Misa AOT runtime — iOS app entry (Phase 4, M7).
 *
 * On iOS the real entry is UIApplicationMain (the M1 target table's
 * `main` entry is the hosted entry; the emitted main() is ignored).
 * The delegate creates the UIKit driver first, then runs misa_rt_init()
 * once and misa_main(), which builds the first screen through the
 * driver.
 *
 * No storyboard: the UI is 100% programmatic via the driver.
 */
#import <UIKit/UIKit.h>
#import <os/log.h>

#import "misa_rt.h"   /* misa_rt_init() */
#import "misa_ui.h"   /* misa_ui_create(), misa_ui_active() */

/* misa_main is defined in the cross-compiled program object
 * (prog_arm64.o in the transfer package). */
extern MVal *misa_main(void);

/* UIKit-driver helper (implemented in ui_uikit.m): gives
 * misa_ui_show_screen a container to attach screens to. */
void misa_ui_set_root_view(MisaUI *ui, void *rootView);

@interface MisaAppDelegate : UIResponder <UIApplicationDelegate>
@property (strong, nonatomic) UIWindow *window;
@end

@implementation MisaAppDelegate

- (BOOL)application:(UIApplication *)app
        didFinishLaunchingWithOptions:(NSDictionary *)opts {
    (void)app; (void)opts;
    self.window = [[UIWindow alloc]
        initWithFrame:[UIScreen mainScreen].bounds];
    UIViewController *root = [UIViewController new];
    root.view.backgroundColor = [UIColor systemBackgroundColor];
    self.window.rootViewController = root;
    [self.window makeKeyAndVisible];

    /* Create the UIKit driver BEFORE any Misa code runs: generated
     * screen/handler code reaches the driver via misa_ui_active().
     * Use misa_ui_active() (the global) — NOT a separate instance —
     * so the root view lands on the same driver the screens use. */
    os_log(OS_LOG_DEFAULT, "misa: didFinishLaunching, creating UI driver");
    MisaUI *ui = misa_ui_active();
    misa_ui_set_root_view(ui, (__bridge void *)root.view);

    os_log(OS_LOG_DEFAULT, "misa: calling misa_rt_init");
    misa_rt_init();   /* once, before any Misa call */
    os_log(OS_LOG_DEFAULT, "misa: calling misa_main");
    misa_main();      /* builds the first screen through the driver */
    os_log(OS_LOG_DEFAULT, "misa: misa_main returned, app alive");
    return YES;
}

@end

int main(int argc, char *argv[]) {
    @autoreleasepool {
        return UIApplicationMain(argc, argv, nil,
                                 NSStringFromClass([MisaAppDelegate class]));
    }
}
