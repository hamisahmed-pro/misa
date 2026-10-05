#!/usr/bin/env python3
"""Generate ios/MisaApp.xcodeproj/project.pbxproj for the Misa iOS template.

The project compiles the Misa runtime sources (from impl/misa/aot/rt/)
and links the per-program object (program/prog_arm64.o, produced on
Linux by `misa build --target aarch64-apple-darwin` and dropped into
the transfer package) into a UIKit app.

Run: python3 gen_xcodeproj.py   (writes project.pbxproj in place)
"""
import hashlib
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "MisaApp.xcodeproj", "project.pbxproj")

# Sources compiled from the repo (relative to ios/MisaApp/).
RT = "../../impl/misa/aot/rt"
SOURCES = [
    ("main.m", RT + "/main.m", "sourcecode.c.objc"),
    ("platform_apple.m", RT + "/platform_apple.m", "sourcecode.c.objc"),
    ("ui_uikit.m", RT + "/ui_uikit.m", "sourcecode.c.objc"),
    ("misa_rt.c", RT + "/misa_rt.c", "sourcecode.c.c"),
    ("misa_model.c", RT + "/misa_model.c", "sourcecode.c.c"),
]
HEADERS = [
    ("misa_rt.h", RT + "/misa_rt.h"),
    ("misa_ui.h", RT + "/misa_ui.h"),
    ("misa_platform.h", RT + "/misa_platform.h"),
]


def uid(name: str) -> str:
    """Stable 24-hex object id from a name."""
    return hashlib.sha256(name.encode()).hexdigest()[:24].upper()


# ---------------------------------------------------------------- ids
ids = {}
for fname, _, _ in SOURCES:
    ids["ref:" + fname] = uid("ref:" + fname)
    ids["build:" + fname] = uid("build:" + fname)
for fname, _ in HEADERS:
    ids["ref:" + fname] = uid("ref:" + fname)
ids.update({
    "ref:prog_arm64.o": uid("ref:prog_arm64.o"),
    "build:prog_arm64.o": uid("build:prog_arm64.o"),
    "ref:Info.plist": uid("ref:Info.plist"),
    "ref:Foundation": uid("ref:Foundation"),
    "ref:UIKit": uid("ref:UIKit"),
    "build:Foundation": uid("build:Foundation"),
    "build:UIKit": uid("build:UIKit"),
    "group:main": uid("group:main"),
    "group:rt": uid("group:rt"),
    "group:program": uid("group:program"),
    "group:frameworks": uid("group:frameworks"),
    "group:products": uid("group:products"),
    "phase:sources": uid("phase:sources"),
    "phase:frameworks": uid("phase:frameworks"),
    "phase:resources": uid("phase:resources"),
    "target": uid("target"),
    "project": uid("project"),
    "configlist:project": uid("configlist:project"),
    "configlist:target": uid("configlist:target"),
    "config:project:debug": uid("config:project:debug"),
    "config:project:release": uid("config:project:release"),
    "config:target:debug": uid("config:target:debug"),
    "config:target:release": uid("config:target:release"),
    "productref": uid("productref"),
})


def q(s: str) -> str:
    return '"%s"' % s


lines = []
W = lines.append

W("// !$*UTF8*$!")
W("{")
W("\tarchiveVersion = 1;")
W("\tclasses = {")
W("\t};")
W("\tobjectVersion = 60;")
W("\tobjects = {")

# ---- PBXBuildFile ----
for fname, _, _ in SOURCES:
    W("\t\t%s /* %s in Sources */ = {isa = PBXBuildFile; fileRef = %s /* %s */; };"
      % (ids["build:" + fname], fname, ids["ref:" + fname], fname))
for fw in ("Foundation", "UIKit"):
    W("\t\t%s /* %s.framework in Frameworks */ = {isa = PBXBuildFile; fileRef = %s /* %s.framework */; };"
      % (ids["build:" + fw], fw, ids["ref:" + fw], fw))
W("\t\t%s /* prog_arm64.o in Frameworks */ = {isa = PBXBuildFile; fileRef = %s /* prog_arm64.o */; };"
  % (ids["build:prog_arm64.o"], ids["ref:prog_arm64.o"]))

# ---- PBXFileReference ----
for fname, path, ftype in SOURCES:
    W("\t\t%s /* %s */ = {isa = PBXFileReference; lastKnownFileType = %s; path = %s; sourceTree = SOURCE_ROOT; };"
      % (ids["ref:" + fname], fname, ftype, q(path)))
for fname, path in HEADERS:
    W("\t\t%s /* %s */ = {isa = PBXFileReference; lastKnownFileType = sourcecode.c.h; path = %s; sourceTree = SOURCE_ROOT; };"
      % (ids["ref:" + fname], fname, q(path)))
W("\t\t%s /* prog_arm64.o */ = {isa = PBXFileReference; lastKnownFileType = compiled.mach-o.objfile; path = %s; sourceTree = SOURCE_ROOT; };"
  % (ids["ref:prog_arm64.o"], q("program/prog_arm64.o")))
W("\t\t%s /* Info.plist */ = {isa = PBXFileReference; lastKnownFileType = text.plist.xml; path = Info.plist; sourceTree = \"<group>\"; };"
  % ids["ref:Info.plist"])
W("\t\t%s /* Foundation.framework */ = {isa = PBXFileReference; lastKnownFileType = wrapper.framework; name = Foundation.framework; path = System/Library/Frameworks/Foundation.framework; sourceTree = SDKROOT; };"
  % ids["ref:Foundation"])
W("\t\t%s /* UIKit.framework */ = {isa = PBXFileReference; lastKnownFileType = wrapper.framework; name = UIKit.framework; path = System/Library/Frameworks/UIKit.framework; sourceTree = SDKROOT; };"
  % ids["ref:UIKit"])
W("\t\t%s /* MisaApp.app */ = {isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = MisaApp.app; sourceTree = BUILT_PRODUCTS_DIR; };"
  % ids["productref"])

# ---- PBXFrameworksBuildPhase / Resources / Sources ----
W("\t\t%s /* Frameworks */ = {isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = ("
  % ids["phase:frameworks"])
for fw in ("Foundation", "UIKit"):
    W("\t\t\t%s /* %s.framework in Frameworks */," % (ids["build:" + fw], fw))
W("\t\t\t%s /* prog_arm64.o in Frameworks */," % ids["build:prog_arm64.o"])
W("\t\t); runOnlyForDeploymentPostprocessing = 0; };")
W("\t\t%s /* Resources */ = {isa = PBXResourcesBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0; };"
  % ids["phase:resources"])
W("\t\t%s /* Sources */ = {isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = ("
  % ids["phase:sources"])
for fname, _, _ in SOURCES:
    W("\t\t\t%s /* %s in Sources */," % (ids["build:" + fname], fname))
W("\t\t); runOnlyForDeploymentPostprocessing = 0; };")

# ---- PBXGroup ----
W("\t\t%s = {isa = PBXGroup; children = (%s, %s, %s, %s, %s); sourceTree = \"<group>\"; };"
  % (ids["group:main"], ids["group:rt"], ids["group:program"],
     ids["group:frameworks"], ids["ref:Info.plist"], ids["group:products"]))
rt_children = ", ".join(
    [ids["ref:" + f] for f, _ in HEADERS] +
    [ids["ref:" + f] for f, _, _ in SOURCES])
W("\t\t%s /* Runtime */ = {isa = PBXGroup; children = (%s); name = Runtime; sourceTree = \"<group>\"; };"
  % (ids["group:rt"], rt_children))
W("\t\t%s /* Program */ = {isa = PBXGroup; children = (%s); name = Program; path = program; sourceTree = \"<group>\"; };"
  % (ids["group:program"], ids["ref:prog_arm64.o"]))
W("\t\t%s /* Frameworks */ = {isa = PBXGroup; children = (%s, %s); name = Frameworks; sourceTree = \"<group>\"; };"
  % (ids["group:frameworks"], ids["ref:Foundation"], ids["ref:UIKit"]))
W("\t\t%s /* Products */ = {isa = PBXGroup; children = (%s); name = Products; sourceTree = \"<group>\"; };"
  % (ids["group:products"], ids["productref"]))

# ---- PBXNativeTarget ----
W("\t\t%s /* MisaApp */ = {isa = PBXNativeTarget; buildConfigurationList = %s /* Build configuration list for PBXNativeTarget \"MisaApp\" */; buildPhases = ("
  % (ids["target"], ids["configlist:target"]))
W("\t\t\t%s /* Resources */," % ids["phase:resources"])
W("\t\t\t%s /* Sources */," % ids["phase:sources"])
W("\t\t\t%s /* Frameworks */," % ids["phase:frameworks"])
W("\t\t); buildRules = (); dependencies = (); name = MisaApp; productName = MisaApp; productReference = %s /* MisaApp.app */; productType = \"com.apple.product-type.application\"; };"
  % ids["productref"])

# ---- PBXProject ----
W("\t\t%s /* Project object */ = {isa = PBXProject; attributes = { LastUpgradeCheck = 1500; TargetAttributes = { %s = { CreatedOnToolsVersion = 15.0; }; }; }; buildConfigurationList = %s /* Build configuration list for PBXProject \"MisaApp\" */; compatibilityVersion = \"Xcode 14.0\"; developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en); mainGroup = %s; productRefGroup = %s /* Products */; projectDirPath = \"\"; projectRoot = \"\"; targets = (%s /* MisaApp */,); };"
  % (ids["project"], ids["target"], ids["configlist:project"],
     ids["group:main"], ids["group:products"], ids["target"]))

# ---- XCBuildConfiguration ----
for cid, name, extra in [
    ("config:project:debug", "Debug", ""),
    ("config:project:release", "Release", ""),
]:
    W("\t\t%s /* %s */ = {isa = XCBuildConfiguration; buildSettings = { ALWAYS_SEARCH_USER_PATHS = NO; CLANG_ENABLE_OBJC_ARC = YES; COPY_PHASE_STRIP = NO; DEBUG_INFORMATION_FORMAT = dwarf; ENABLE_STRICT_OBJC_MSGSEND = YES; GCC_C_LANGUAGE_STANDARD = gnu11; IPHONEOS_DEPLOYMENT_TARGET = 15.0; ONLY_ACTIVE_ARCH = YES; SDKROOT = iphoneos; }; name = %s; };"
      % (ids[cid], name, name))

for cid, name in [("config:target:debug", "Debug"),
                  ("config:target:release", "Release")]:
    dbg = "GCC_OPTIMIZATION_LEVEL = 0; " if name == "Debug" else ""
    W("\t\t%s /* %s */ = {isa = XCBuildConfiguration; buildSettings = { ARCHS = arm64; CODE_SIGN_STYLE = Automatic; DEVELOPMENT_TEAM = \"\"; %sINFOPLIST_FILE = \"$(SRCROOT)/Info.plist\"; LD_RUNPATH_SEARCH_PATHS = (\"$(inherited)\", \"@executable_path/Frameworks\"); PRODUCT_BUNDLE_IDENTIFIER = com.misa.misaapp; PRODUCT_NAME = \"$(TARGET_NAME)\"; SWIFT_VERSION = 5.0; TARGETED_DEVICE_FAMILY = \"1,2\"; }; name = %s; };"
      % (ids[cid], name, dbg, name))

# ---- XCConfigurationList ----
W("\t\t%s /* Build configuration list for PBXProject \"MisaApp\" */ = {isa = XCConfigurationList; buildConfigurations = (%s /* Debug */, %s /* Release */,); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release; };"
  % (ids["configlist:project"], ids["config:project:debug"],
     ids["config:project:release"]))
W("\t\t%s /* Build configuration list for PBXNativeTarget \"MisaApp\" */ = {isa = XCConfigurationList; buildConfigurations = (%s /* Debug */, %s /* Release */,); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release; };"
  % (ids["configlist:target"], ids["config:target:debug"],
     ids["config:target:release"]))

W("\t};")
W("\trootObject = %s /* Project object */;" % ids["project"])
W("}")

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    f.write("\n".join(lines) + "\n")
print("wrote", OUT)

# ---- sanity: every referenced id is defined ----
import re
text = open(OUT).read()
defined = set(re.findall(r"^\t\t([0-9A-F]{24}) /\*", text, re.M))
used = set(re.findall(r"= ([0-9A-F]{24}) /\*", text)) | \
       set(re.findall(r"\(([0-9A-F]{24}) /\*", text)) | \
       set(re.findall(r", ([0-9A-F]{24}) /\*", text))
missing = used - defined - {ids["project"]}
print("defined:", len(defined), "missing refs:", sorted(missing) or "none")
