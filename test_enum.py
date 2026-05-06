# test_enum.py
try:
    from maxapi.types.attachments import AttachmentType
    print(f"✅ Found AttachmentType: {AttachmentType}")
    print(f"Attributes: {dir(AttachmentType)}")
except ImportError:
    pass

try:
    from maxapi.enums import UploadType
    print(f"✅ Found UploadType in enums: {UploadType}")
    print(f"Attributes: {dir(UploadType)}")
except ImportError:
    pass

# Check all modules
import maxapi
import pkgutil
print("\n=== All maxapi modules ===")
for importer, modname, ispkg in pkgutil.walk_packages(maxapi.__path__, prefix='maxapi.'):
    if 'enum' in modname.lower() or 'type' in modname.lower():
        print(modname)