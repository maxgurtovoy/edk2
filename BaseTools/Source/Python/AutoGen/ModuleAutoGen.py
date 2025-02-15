## @file
# Create makefile for MS nmake and GNU make
#
# Copyright (c) 2019, Intel Corporation. All rights reserved.<BR>
# SPDX-License-Identifier: BSD-2-Clause-Patent
#
from __future__ import absolute_import
from AutoGen.AutoGen import AutoGen
from Common.LongFilePathSupport import CopyLongFilePath
from Common.BuildToolError import *
from Common.DataType import *
from Common.Misc import *
from Common.StringUtils import NormPath,GetSplitList
from collections import defaultdict
from Workspace.WorkspaceCommon import OrderedListDict
import os.path as path
import copy
import hashlib
from . import InfSectionParser
from . import GenC
from . import GenMake
from . import GenDepex
from io import BytesIO
from GenPatchPcdTable.GenPatchPcdTable import parsePcdInfoFromMapFile
from Workspace.MetaFileCommentParser import UsageList
from .GenPcdDb import CreatePcdDatabaseCode
from Common.caching import cached_class_function
from AutoGen.ModuleAutoGenHelper import PlatformInfo,WorkSpaceInfo

## Mapping Makefile type
gMakeTypeMap = {TAB_COMPILER_MSFT:"nmake", "GCC":"gmake"}
#
# Regular expression for finding Include Directories, the difference between MSFT and INTEL/GCC/RVCT
# is the former use /I , the Latter used -I to specify include directories
#
gBuildOptIncludePatternMsft = re.compile(r"(?:.*?)/I[ \t]*([^ ]*)", re.MULTILINE | re.DOTALL)
gBuildOptIncludePatternOther = re.compile(r"(?:.*?)-I[ \t]*([^ ]*)", re.MULTILINE | re.DOTALL)

## default file name for AutoGen
gAutoGenCodeFileName = "AutoGen.c"
gAutoGenHeaderFileName = "AutoGen.h"
gAutoGenStringFileName = "%(module_name)sStrDefs.h"
gAutoGenStringFormFileName = "%(module_name)sStrDefs.hpk"
gAutoGenDepexFileName = "%(module_name)s.depex"
gAutoGenImageDefFileName = "%(module_name)sImgDefs.h"
gAutoGenIdfFileName = "%(module_name)sIdf.hpk"
gInfSpecVersion = "0x00010017"

#
# Match name = variable
#
gEfiVarStoreNamePattern = re.compile("\s*name\s*=\s*(\w+)")
#
# The format of guid in efivarstore statement likes following and must be correct:
# guid = {0xA04A27f4, 0xDF00, 0x4D42, {0xB5, 0x52, 0x39, 0x51, 0x13, 0x02, 0x11, 0x3D}}
#
gEfiVarStoreGuidPattern = re.compile("\s*guid\s*=\s*({.*?{.*?}\s*})")

#
# Template string to generic AsBuilt INF
#
gAsBuiltInfHeaderString = TemplateString("""${header_comments}

# DO NOT EDIT
# FILE auto-generated

[Defines]
  INF_VERSION                = ${module_inf_version}
  BASE_NAME                  = ${module_name}
  FILE_GUID                  = ${module_guid}
  MODULE_TYPE                = ${module_module_type}${BEGIN}
  VERSION_STRING             = ${module_version_string}${END}${BEGIN}
  PCD_IS_DRIVER              = ${pcd_is_driver_string}${END}${BEGIN}
  UEFI_SPECIFICATION_VERSION = ${module_uefi_specification_version}${END}${BEGIN}
  PI_SPECIFICATION_VERSION   = ${module_pi_specification_version}${END}${BEGIN}
  ENTRY_POINT                = ${module_entry_point}${END}${BEGIN}
  UNLOAD_IMAGE               = ${module_unload_image}${END}${BEGIN}
  CONSTRUCTOR                = ${module_constructor}${END}${BEGIN}
  DESTRUCTOR                 = ${module_destructor}${END}${BEGIN}
  SHADOW                     = ${module_shadow}${END}${BEGIN}
  PCI_VENDOR_ID              = ${module_pci_vendor_id}${END}${BEGIN}
  PCI_DEVICE_ID              = ${module_pci_device_id}${END}${BEGIN}
  PCI_CLASS_CODE             = ${module_pci_class_code}${END}${BEGIN}
  PCI_REVISION               = ${module_pci_revision}${END}${BEGIN}
  BUILD_NUMBER               = ${module_build_number}${END}${BEGIN}
  SPEC                       = ${module_spec}${END}${BEGIN}
  UEFI_HII_RESOURCE_SECTION  = ${module_uefi_hii_resource_section}${END}${BEGIN}
  MODULE_UNI_FILE            = ${module_uni_file}${END}

[Packages.${module_arch}]${BEGIN}
  ${package_item}${END}

[Binaries.${module_arch}]${BEGIN}
  ${binary_item}${END}

[PatchPcd.${module_arch}]${BEGIN}
  ${patchablepcd_item}
${END}

[Protocols.${module_arch}]${BEGIN}
  ${protocol_item}
${END}

[Ppis.${module_arch}]${BEGIN}
  ${ppi_item}
${END}

[Guids.${module_arch}]${BEGIN}
  ${guid_item}
${END}

[PcdEx.${module_arch}]${BEGIN}
  ${pcd_item}
${END}

[LibraryClasses.${module_arch}]
## @LIB_INSTANCES${BEGIN}
#  ${libraryclasses_item}${END}

${depexsection_item}

${userextension_tianocore_item}

${tail_comments}

[BuildOptions.${module_arch}]
## @AsBuilt${BEGIN}
##   ${flags_item}${END}
""")
#
# extend lists contained in a dictionary with lists stored in another dictionary
# if CopyToDict is not derived from DefaultDict(list) then this may raise exception
#
def ExtendCopyDictionaryLists(CopyToDict, CopyFromDict):
    for Key in CopyFromDict:
        CopyToDict[Key].extend(CopyFromDict[Key])

# Create a directory specified by a set of path elements and return the full path
def _MakeDir(PathList):
    RetVal = path.join(*PathList)
    CreateDirectory(RetVal)
    return RetVal

#
# Convert string to C format array
#
def _ConvertStringToByteArray(Value):
    Value = Value.strip()
    if not Value:
        return None
    if Value[0] == '{':
        if not Value.endswith('}'):
            return None
        Value = Value.replace(' ', '').replace('{', '').replace('}', '')
        ValFields = Value.split(',')
        try:
            for Index in range(len(ValFields)):
                ValFields[Index] = str(int(ValFields[Index], 0))
        except ValueError:
            return None
        Value = '{' + ','.join(ValFields) + '}'
        return Value

    Unicode = False
    if Value.startswith('L"'):
        if not Value.endswith('"'):
            return None
        Value = Value[1:]
        Unicode = True
    elif not Value.startswith('"') or not Value.endswith('"'):
        return None

    Value = eval(Value)         # translate escape character
    NewValue = '{'
    for Index in range(0, len(Value)):
        if Unicode:
            NewValue = NewValue + str(ord(Value[Index]) % 0x10000) + ','
        else:
            NewValue = NewValue + str(ord(Value[Index]) % 0x100) + ','
    Value = NewValue + '0}'
    return Value

## ModuleAutoGen class
#
# This class encapsules the AutoGen behaviors for the build tools. In addition to
# the generation of AutoGen.h and AutoGen.c, it will generate *.depex file according
# to the [depex] section in module's inf file.
#
class ModuleAutoGen(AutoGen):
    # call super().__init__ then call the worker function with different parameter count
    def __init__(self, Workspace, MetaFile, Target, Toolchain, Arch, *args, **kwargs):
        if not hasattr(self, "_Init"):
            self._InitWorker(Workspace, MetaFile, Target, Toolchain, Arch, *args)
            self._Init = True

    ## Cache the timestamps of metafiles of every module in a class attribute
    #
    TimeDict = {}

    def __new__(cls, Workspace, MetaFile, Target, Toolchain, Arch, *args, **kwargs):
#         check if this module is employed by active platform
        if not PlatformInfo(Workspace, args[0], Target, Toolchain, Arch,args[-1]).ValidModule(MetaFile):
            EdkLogger.verbose("Module [%s] for [%s] is not employed by active platform\n" \
                              % (MetaFile, Arch))
            return None
        return super(ModuleAutoGen, cls).__new__(cls, Workspace, MetaFile, Target, Toolchain, Arch, *args, **kwargs)

    ## Initialize ModuleAutoGen
    #
    #   @param      Workspace           EdkIIWorkspaceBuild object
    #   @param      ModuleFile          The path of module file
    #   @param      Target              Build target (DEBUG, RELEASE)
    #   @param      Toolchain           Name of tool chain
    #   @param      Arch                The arch the module supports
    #   @param      PlatformFile        Platform meta-file
    #
    def _InitWorker(self, Workspace, ModuleFile, Target, Toolchain, Arch, PlatformFile,DataPipe):
        EdkLogger.debug(EdkLogger.DEBUG_9, "AutoGen module [%s] [%s]" % (ModuleFile, Arch))
        GlobalData.gProcessingFile = "%s [%s, %s, %s]" % (ModuleFile, Arch, Toolchain, Target)

        self.Workspace = Workspace
        self.WorkspaceDir = ""
        self.PlatformInfo = None
        self.DataPipe = DataPipe
        self.__init_platform_info__()
        self.MetaFile = ModuleFile
        self.SourceDir = self.MetaFile.SubDir
        self.SourceDir = mws.relpath(self.SourceDir, self.WorkspaceDir)

        self.ToolChain = Toolchain
        self.BuildTarget = Target
        self.Arch = Arch
        self.ToolChainFamily = self.PlatformInfo.ToolChainFamily
        self.BuildRuleFamily = self.PlatformInfo.BuildRuleFamily

        self.IsCodeFileCreated = False
        self.IsAsBuiltInfCreated = False
        self.DepexGenerated = False

        self.BuildDatabase = self.Workspace.BuildDatabase
        self.BuildRuleOrder = None
        self.BuildTime      = 0

        self._GuidComments = OrderedListDict()
        self._ProtocolComments = OrderedListDict()
        self._PpiComments = OrderedListDict()
        self._BuildTargets            = None
        self._IntroBuildTargetList    = None
        self._FinalBuildTargetList    = None
        self._FileTypes               = None

        self.AutoGenDepSet = set()
        self.ReferenceModules = []
        self.ConstPcd                  = {}

    def __init_platform_info__(self):
        pinfo = self.DataPipe.Get("P_Info")
        self.WorkspaceDir = pinfo.get("WorkspaceDir")
        self.PlatformInfo = PlatformInfo(self.Workspace,pinfo.get("ActivePlatform"),pinfo.get("Target"),pinfo.get("ToolChain"),pinfo.get("Arch"),self.DataPipe)
    ## hash() operator of ModuleAutoGen
    #
    #  The module file path and arch string will be used to represent
    #  hash value of this object
    #
    #   @retval   int Hash value of the module file path and arch
    #
    @cached_class_function
    def __hash__(self):
        return hash((self.MetaFile, self.Arch))
    def __repr__(self):
        return "%s [%s]" % (self.MetaFile, self.Arch)

    # Get FixedAtBuild Pcds of this Module
    @cached_property
    def FixedAtBuildPcds(self):
        RetVal = []
        for Pcd in self.ModulePcdList:
            if Pcd.Type != TAB_PCDS_FIXED_AT_BUILD:
                continue
            if Pcd not in RetVal:
                RetVal.append(Pcd)
        return RetVal

    @cached_property
    def FixedVoidTypePcds(self):
        RetVal = {}
        for Pcd in self.FixedAtBuildPcds:
            if Pcd.DatumType == TAB_VOID:
                if '.'.join((Pcd.TokenSpaceGuidCName, Pcd.TokenCName)) not in RetVal:
                    RetVal['.'.join((Pcd.TokenSpaceGuidCName, Pcd.TokenCName))] = Pcd.DefaultValue
        return RetVal

    @property
    def UniqueBaseName(self):
        ModuleNames = self.DataPipe.Get("M_Name")
        if not ModuleNames:
            return self.Name
        return ModuleNames.get(self.Name,self.Name)

    # Macros could be used in build_rule.txt (also Makefile)
    @cached_property
    def Macros(self):
        return OrderedDict((
            ("WORKSPACE" ,self.WorkspaceDir),
            ("MODULE_NAME" ,self.Name),
            ("MODULE_NAME_GUID" ,self.UniqueBaseName),
            ("MODULE_GUID" ,self.Guid),
            ("MODULE_VERSION" ,self.Version),
            ("MODULE_TYPE" ,self.ModuleType),
            ("MODULE_FILE" ,str(self.MetaFile)),
            ("MODULE_FILE_BASE_NAME" ,self.MetaFile.BaseName),
            ("MODULE_RELATIVE_DIR" ,self.SourceDir),
            ("MODULE_DIR" ,self.SourceDir),
            ("BASE_NAME" ,self.Name),
            ("ARCH" ,self.Arch),
            ("TOOLCHAIN" ,self.ToolChain),
            ("TOOLCHAIN_TAG" ,self.ToolChain),
            ("TOOL_CHAIN_TAG" ,self.ToolChain),
            ("TARGET" ,self.BuildTarget),
            ("BUILD_DIR" ,self.PlatformInfo.BuildDir),
            ("BIN_DIR" ,os.path.join(self.PlatformInfo.BuildDir, self.Arch)),
            ("LIB_DIR" ,os.path.join(self.PlatformInfo.BuildDir, self.Arch)),
            ("MODULE_BUILD_DIR" ,self.BuildDir),
            ("OUTPUT_DIR" ,self.OutputDir),
            ("DEBUG_DIR" ,self.DebugDir),
            ("DEST_DIR_OUTPUT" ,self.OutputDir),
            ("DEST_DIR_DEBUG" ,self.DebugDir),
            ("PLATFORM_NAME" ,self.PlatformInfo.Name),
            ("PLATFORM_GUID" ,self.PlatformInfo.Guid),
            ("PLATFORM_VERSION" ,self.PlatformInfo.Version),
            ("PLATFORM_RELATIVE_DIR" ,self.PlatformInfo.SourceDir),
            ("PLATFORM_DIR" ,mws.join(self.WorkspaceDir, self.PlatformInfo.SourceDir)),
            ("PLATFORM_OUTPUT_DIR" ,self.PlatformInfo.OutputDir),
            ("FFS_OUTPUT_DIR" ,self.FfsOutputDir)
            ))

    ## Return the module build data object
    @cached_property
    def Module(self):
        return self.BuildDatabase[self.MetaFile, self.Arch, self.BuildTarget, self.ToolChain]

    ## Return the module name
    @cached_property
    def Name(self):
        return self.Module.BaseName

    ## Return the module DxsFile if exist
    @cached_property
    def DxsFile(self):
        return self.Module.DxsFile

    ## Return the module meta-file GUID
    @cached_property
    def Guid(self):
        #
        # To build same module more than once, the module path with FILE_GUID overridden has
        # the file name FILE_GUIDmodule.inf, but the relative path (self.MetaFile.File) is the real path
        # in DSC. The overridden GUID can be retrieved from file name
        #
        if os.path.basename(self.MetaFile.File) != os.path.basename(self.MetaFile.Path):
            #
            # Length of GUID is 36
            #
            return os.path.basename(self.MetaFile.Path)[:36]
        return self.Module.Guid

    ## Return the module version
    @cached_property
    def Version(self):
        return self.Module.Version

    ## Return the module type
    @cached_property
    def ModuleType(self):
        return self.Module.ModuleType

    ## Return the component type (for Edk.x style of module)
    @cached_property
    def ComponentType(self):
        return self.Module.ComponentType

    ## Return the build type
    @cached_property
    def BuildType(self):
        return self.Module.BuildType

    ## Return the PCD_IS_DRIVER setting
    @cached_property
    def PcdIsDriver(self):
        return self.Module.PcdIsDriver

    ## Return the autogen version, i.e. module meta-file version
    @cached_property
    def AutoGenVersion(self):
        return self.Module.AutoGenVersion

    ## Check if the module is library or not
    @cached_property
    def IsLibrary(self):
        return bool(self.Module.LibraryClass)

    ## Check if the module is binary module or not
    @cached_property
    def IsBinaryModule(self):
        return self.Module.IsBinaryModule

    ## Return the directory to store intermediate files of the module
    @cached_property
    def BuildDir(self):
        return _MakeDir((
                                    self.PlatformInfo.BuildDir,
                                    self.Arch,
                                    self.SourceDir,
                                    self.MetaFile.BaseName
            ))

    ## Return the directory to store the intermediate object files of the module
    @cached_property
    def OutputDir(self):
        return _MakeDir((self.BuildDir, "OUTPUT"))

    ## Return the directory path to store ffs file
    @cached_property
    def FfsOutputDir(self):
        if GlobalData.gFdfParser:
            return path.join(self.PlatformInfo.BuildDir, TAB_FV_DIRECTORY, "Ffs", self.Guid + self.Name)
        return ''

    ## Return the directory to store auto-gened source files of the module
    @cached_property
    def DebugDir(self):
        return _MakeDir((self.BuildDir, "DEBUG"))

    ## Return the path of custom file
    @cached_property
    def CustomMakefile(self):
        RetVal = {}
        for Type in self.Module.CustomMakefile:
            MakeType = gMakeTypeMap[Type] if Type in gMakeTypeMap else 'nmake'
            File = os.path.join(self.SourceDir, self.Module.CustomMakefile[Type])
            RetVal[MakeType] = File
        return RetVal

    ## Return the directory of the makefile
    #
    #   @retval     string  The directory string of module's makefile
    #
    @cached_property
    def MakeFileDir(self):
        return self.BuildDir

    ## Return build command string
    #
    #   @retval     string  Build command string
    #
    @cached_property
    def BuildCommand(self):
        return self.PlatformInfo.BuildCommand

    ## Get object list of all packages the module and its dependent libraries belong to
    #
    #   @retval     list    The list of package object
    #
    @cached_property
    def DerivedPackageList(self):
        PackageList = []
        for M in [self.Module] + self.DependentLibraryList:
            for Package in M.Packages:
                if Package in PackageList:
                    continue
                PackageList.append(Package)
        return PackageList

    ## Get the depex string
    #
    # @return : a string contain all depex expression.
    def _GetDepexExpresionString(self):
        DepexStr = ''
        DepexList = []
        ## DPX_SOURCE IN Define section.
        if self.Module.DxsFile:
            return DepexStr
        for M in [self.Module] + self.DependentLibraryList:
            Filename = M.MetaFile.Path
            InfObj = InfSectionParser.InfSectionParser(Filename)
            DepexExpressionList = InfObj.GetDepexExpresionList()
            for DepexExpression in DepexExpressionList:
                for key in DepexExpression:
                    Arch, ModuleType = key
                    DepexExpr = [x for x in DepexExpression[key] if not str(x).startswith('#')]
                    # the type of build module is USER_DEFINED.
                    # All different DEPEX section tags would be copied into the As Built INF file
                    # and there would be separate DEPEX section tags
                    if self.ModuleType.upper() == SUP_MODULE_USER_DEFINED or self.ModuleType.upper() == SUP_MODULE_HOST_APPLICATION:
                        if (Arch.upper() == self.Arch.upper()) and (ModuleType.upper() != TAB_ARCH_COMMON):
                            DepexList.append({(Arch, ModuleType): DepexExpr})
                    else:
                        if Arch.upper() == TAB_ARCH_COMMON or \
                          (Arch.upper() == self.Arch.upper() and \
                          ModuleType.upper() in [TAB_ARCH_COMMON, self.ModuleType.upper()]):
                            DepexList.append({(Arch, ModuleType): DepexExpr})

        #the type of build module is USER_DEFINED.
        if self.ModuleType.upper() == SUP_MODULE_USER_DEFINED or self.ModuleType.upper() == SUP_MODULE_HOST_APPLICATION:
            for Depex in DepexList:
                for key in Depex:
                    DepexStr += '[Depex.%s.%s]\n' % key
                    DepexStr += '\n'.join('# '+ val for val in Depex[key])
                    DepexStr += '\n\n'
            if not DepexStr:
                return '[Depex.%s]\n' % self.Arch
            return DepexStr

        #the type of build module not is USER_DEFINED.
        Count = 0
        for Depex in DepexList:
            Count += 1
            if DepexStr != '':
                DepexStr += ' AND '
            DepexStr += '('
            for D in Depex.values():
                DepexStr += ' '.join(val for val in D)
            Index = DepexStr.find('END')
            if Index > -1 and Index == len(DepexStr) - 3:
                DepexStr = DepexStr[:-3]
            DepexStr = DepexStr.strip()
            DepexStr += ')'
        if Count == 1:
            DepexStr = DepexStr.lstrip('(').rstrip(')').strip()
        if not DepexStr:
            return '[Depex.%s]\n' % self.Arch
        return '[Depex.%s]\n#  ' % self.Arch + DepexStr

    ## Merge dependency expression
    #
    #   @retval     list    The token list of the dependency expression after parsed
    #
    @cached_property
    def DepexList(self):
        if self.DxsFile or self.IsLibrary or TAB_DEPENDENCY_EXPRESSION_FILE in self.FileTypes:
            return {}

        DepexList = []
        #
        # Append depex from dependent libraries, if not "BEFORE", "AFTER" expression
        #
        FixedVoidTypePcds = {}
        for M in [self] + self.LibraryAutoGenList:
            FixedVoidTypePcds.update(M.FixedVoidTypePcds)
        for M in [self] + self.LibraryAutoGenList:
            Inherited = False
            for D in M.Module.Depex[self.Arch, self.ModuleType]:
                if DepexList != []:
                    DepexList.append('AND')
                DepexList.append('(')
                #replace D with value if D is FixedAtBuild PCD
                NewList = []
                for item in D:
                    if '.' not in item:
                        NewList.append(item)
                    else:
                        try:
                            Value = FixedVoidTypePcds[item]
                            if len(Value.split(',')) != 16:
                                EdkLogger.error("build", FORMAT_INVALID,
                                                "{} used in [Depex] section should be used as FixedAtBuild type and VOID* datum type and 16 bytes in the module.".format(item))
                            NewList.append(Value)
                        except:
                            EdkLogger.error("build", FORMAT_INVALID, "{} used in [Depex] section should be used as FixedAtBuild type and VOID* datum type in the module.".format(item))

                DepexList.extend(NewList)
                if DepexList[-1] == 'END':  # no need of a END at this time
                    DepexList.pop()
                DepexList.append(')')
                Inherited = True
            if Inherited:
                EdkLogger.verbose("DEPEX[%s] (+%s) = %s" % (self.Name, M.Module.BaseName, DepexList))
            if 'BEFORE' in DepexList or 'AFTER' in DepexList:
                break
            if len(DepexList) > 0:
                EdkLogger.verbose('')
        return {self.ModuleType:DepexList}

    ## Merge dependency expression
    #
    #   @retval     list    The token list of the dependency expression after parsed
    #
    @cached_property
    def DepexExpressionDict(self):
        if self.DxsFile or self.IsLibrary or TAB_DEPENDENCY_EXPRESSION_FILE in self.FileTypes:
            return {}

        DepexExpressionString = ''
        #
        # Append depex from dependent libraries, if not "BEFORE", "AFTER" expresion
        #
        for M in [self.Module] + self.DependentLibraryList:
            Inherited = False
            for D in M.DepexExpression[self.Arch, self.ModuleType]:
                if DepexExpressionString != '':
                    DepexExpressionString += ' AND '
                DepexExpressionString += '('
                DepexExpressionString += D
                DepexExpressionString = DepexExpressionString.rstrip('END').strip()
                DepexExpressionString += ')'
                Inherited = True
            if Inherited:
                EdkLogger.verbose("DEPEX[%s] (+%s) = %s" % (self.Name, M.BaseName, DepexExpressionString))
            if 'BEFORE' in DepexExpressionString or 'AFTER' in DepexExpressionString:
                break
        if len(DepexExpressionString) > 0:
            EdkLogger.verbose('')

        return {self.ModuleType:DepexExpressionString}

    # Get the tiano core user extension, it is contain dependent library.
    # @retval: a list contain tiano core userextension.
    #
    def _GetTianoCoreUserExtensionList(self):
        TianoCoreUserExtentionList = []
        for M in [self.Module] + self.DependentLibraryList:
            Filename = M.MetaFile.Path
            InfObj = InfSectionParser.InfSectionParser(Filename)
            TianoCoreUserExtenList = InfObj.GetUserExtensionTianoCore()
            for TianoCoreUserExtent in TianoCoreUserExtenList:
                for Section in TianoCoreUserExtent:
                    ItemList = Section.split(TAB_SPLIT)
                    Arch = self.Arch
                    if len(ItemList) == 4:
                        Arch = ItemList[3]
                    if Arch.upper() == TAB_ARCH_COMMON or Arch.upper() == self.Arch.upper():
                        TianoCoreList = []
                        TianoCoreList.extend([TAB_SECTION_START + Section + TAB_SECTION_END])
                        TianoCoreList.extend(TianoCoreUserExtent[Section][:])
                        TianoCoreList.append('\n')
                        TianoCoreUserExtentionList.append(TianoCoreList)

        return TianoCoreUserExtentionList

    ## Return the list of specification version required for the module
    #
    #   @retval     list    The list of specification defined in module file
    #
    @cached_property
    def Specification(self):
        return self.Module.Specification

    ## Tool option for the module build
    #
    #   @param      PlatformInfo    The object of PlatformBuildInfo
    #   @retval     dict            The dict containing valid options
    #
    @cached_property
    def BuildOption(self):
        RetVal, self.BuildRuleOrder = self.PlatformInfo.ApplyBuildOption(self.Module)
        if self.BuildRuleOrder:
            self.BuildRuleOrder = ['.%s' % Ext for Ext in self.BuildRuleOrder.split()]
        return RetVal

    ## Get include path list from tool option for the module build
    #
    #   @retval     list            The include path list
    #
    @cached_property
    def BuildOptionIncPathList(self):
        #
        # Regular expression for finding Include Directories, the difference between MSFT and INTEL/GCC/RVCT
        # is the former use /I , the Latter used -I to specify include directories
        #
        if self.PlatformInfo.ToolChainFamily in (TAB_COMPILER_MSFT):
            BuildOptIncludeRegEx = gBuildOptIncludePatternMsft
        elif self.PlatformInfo.ToolChainFamily in ('INTEL', 'GCC', 'RVCT'):
            BuildOptIncludeRegEx = gBuildOptIncludePatternOther
        else:
            #
            # New ToolChainFamily, don't known whether there is option to specify include directories
            #
            return []

        RetVal = []
        for Tool in ('CC', 'PP', 'VFRPP', 'ASLPP', 'ASLCC', 'APP', 'ASM'):
            try:
                FlagOption = self.BuildOption[Tool]['FLAGS']
            except KeyError:
                FlagOption = ''

            if self.ToolChainFamily != 'RVCT':
                IncPathList = [NormPath(Path, self.Macros) for Path in BuildOptIncludeRegEx.findall(FlagOption)]
            else:
                #
                # RVCT may specify a list of directory seperated by commas
                #
                IncPathList = []
                for Path in BuildOptIncludeRegEx.findall(FlagOption):
                    PathList = GetSplitList(Path, TAB_COMMA_SPLIT)
                    IncPathList.extend(NormPath(PathEntry, self.Macros) for PathEntry in PathList)

            #
            # EDK II modules must not reference header files outside of the packages they depend on or
            # within the module's directory tree. Report error if violation.
            #
            if GlobalData.gDisableIncludePathCheck == False:
                for Path in IncPathList:
                    if (Path not in self.IncludePathList) and (CommonPath([Path, self.MetaFile.Dir]) != self.MetaFile.Dir):
                        ErrMsg = "The include directory for the EDK II module in this line is invalid %s specified in %s FLAGS '%s'" % (Path, Tool, FlagOption)
                        EdkLogger.error("build",
                                        PARAMETER_INVALID,
                                        ExtraData=ErrMsg,
                                        File=str(self.MetaFile))
            RetVal += IncPathList
        return RetVal

    ## Return a list of files which can be built from source
    #
    #  What kind of files can be built is determined by build rules in
    #  $(CONF_DIRECTORY)/build_rule.txt and toolchain family.
    #
    @cached_property
    def SourceFileList(self):
        RetVal = []
        ToolChainTagSet = {"", TAB_STAR, self.ToolChain}
        ToolChainFamilySet = {"", TAB_STAR, self.ToolChainFamily, self.BuildRuleFamily}
        for F in self.Module.Sources:
            # match tool chain
            if F.TagName not in ToolChainTagSet:
                EdkLogger.debug(EdkLogger.DEBUG_9, "The toolchain [%s] for processing file [%s] is found, "
                                "but [%s] is currently used" % (F.TagName, str(F), self.ToolChain))
                continue
            # match tool chain family or build rule family
            if F.ToolChainFamily not in ToolChainFamilySet:
                EdkLogger.debug(
                            EdkLogger.DEBUG_0,
                            "The file [%s] must be built by tools of [%s], " \
                            "but current toolchain family is [%s], buildrule family is [%s]" \
                                % (str(F), F.ToolChainFamily, self.ToolChainFamily, self.BuildRuleFamily))
                continue

            # add the file path into search path list for file including
            if F.Dir not in self.IncludePathList:
                self.IncludePathList.insert(0, F.Dir)
            RetVal.append(F)

        self._MatchBuildRuleOrder(RetVal)

        for F in RetVal:
            self._ApplyBuildRule(F, TAB_UNKNOWN_FILE)
        return RetVal

    def _MatchBuildRuleOrder(self, FileList):
        Order_Dict = {}
        self.BuildOption
        for SingleFile in FileList:
            if self.BuildRuleOrder and SingleFile.Ext in self.BuildRuleOrder and SingleFile.Ext in self.BuildRules:
                key = SingleFile.Path.rsplit(SingleFile.Ext,1)[0]
                if key in Order_Dict:
                    Order_Dict[key].append(SingleFile.Ext)
                else:
                    Order_Dict[key] = [SingleFile.Ext]

        RemoveList = []
        for F in Order_Dict:
            if len(Order_Dict[F]) > 1:
                Order_Dict[F].sort(key=lambda i: self.BuildRuleOrder.index(i))
                for Ext in Order_Dict[F][1:]:
                    RemoveList.append(F + Ext)

        for item in RemoveList:
            FileList.remove(item)

        return FileList

    ## Return the list of unicode files
    @cached_property
    def UnicodeFileList(self):
        return self.FileTypes.get(TAB_UNICODE_FILE,[])

    ## Return the list of vfr files
    @cached_property
    def VfrFileList(self):
        return self.FileTypes.get(TAB_VFR_FILE, [])

    ## Return the list of Image Definition files
    @cached_property
    def IdfFileList(self):
        return self.FileTypes.get(TAB_IMAGE_FILE,[])

    ## Return a list of files which can be built from binary
    #
    #  "Build" binary files are just to copy them to build directory.
    #
    #   @retval     list            The list of files which can be built later
    #
    @cached_property
    def BinaryFileList(self):
        RetVal = []
        for F in self.Module.Binaries:
            if F.Target not in [TAB_ARCH_COMMON, TAB_STAR] and F.Target != self.BuildTarget:
                continue
            RetVal.append(F)
            self._ApplyBuildRule(F, F.Type, BinaryFileList=RetVal)
        return RetVal

    @cached_property
    def BuildRules(self):
        RetVal = {}
        BuildRuleDatabase = self.PlatformInfo.BuildRule
        for Type in BuildRuleDatabase.FileTypeList:
            #first try getting build rule by BuildRuleFamily
            RuleObject = BuildRuleDatabase[Type, self.BuildType, self.Arch, self.BuildRuleFamily]
            if not RuleObject:
                # build type is always module type, but ...
                if self.ModuleType != self.BuildType:
                    RuleObject = BuildRuleDatabase[Type, self.ModuleType, self.Arch, self.BuildRuleFamily]
            #second try getting build rule by ToolChainFamily
            if not RuleObject:
                RuleObject = BuildRuleDatabase[Type, self.BuildType, self.Arch, self.ToolChainFamily]
                if not RuleObject:
                    # build type is always module type, but ...
                    if self.ModuleType != self.BuildType:
                        RuleObject = BuildRuleDatabase[Type, self.ModuleType, self.Arch, self.ToolChainFamily]
            if not RuleObject:
                continue
            RuleObject = RuleObject.Instantiate(self.Macros)
            RetVal[Type] = RuleObject
            for Ext in RuleObject.SourceFileExtList:
                RetVal[Ext] = RuleObject
        return RetVal

    def _ApplyBuildRule(self, File, FileType, BinaryFileList=None):
        if self._BuildTargets is None:
            self._IntroBuildTargetList = set()
            self._FinalBuildTargetList = set()
            self._BuildTargets = defaultdict(set)
            self._FileTypes = defaultdict(set)

        if not BinaryFileList:
            BinaryFileList = self.BinaryFileList

        SubDirectory = os.path.join(self.OutputDir, File.SubDir)
        if not os.path.exists(SubDirectory):
            CreateDirectory(SubDirectory)
        LastTarget = None
        RuleChain = set()
        SourceList = [File]
        Index = 0
        #
        # Make sure to get build rule order value
        #
        self.BuildOption

        while Index < len(SourceList):
            Source = SourceList[Index]
            Index = Index + 1

            if Source != File:
                CreateDirectory(Source.Dir)

            if File.IsBinary and File == Source and File in BinaryFileList:
                # Skip all files that are not binary libraries
                if not self.IsLibrary:
                    continue
                RuleObject = self.BuildRules[TAB_DEFAULT_BINARY_FILE]
            elif FileType in self.BuildRules:
                RuleObject = self.BuildRules[FileType]
            elif Source.Ext in self.BuildRules:
                RuleObject = self.BuildRules[Source.Ext]
            else:
                # stop at no more rules
                if LastTarget:
                    self._FinalBuildTargetList.add(LastTarget)
                break

            FileType = RuleObject.SourceFileType
            self._FileTypes[FileType].add(Source)

            # stop at STATIC_LIBRARY for library
            if self.IsLibrary and FileType == TAB_STATIC_LIBRARY:
                if LastTarget:
                    self._FinalBuildTargetList.add(LastTarget)
                break

            Target = RuleObject.Apply(Source, self.BuildRuleOrder)
            if not Target:
                if LastTarget:
                    self._FinalBuildTargetList.add(LastTarget)
                break
            elif not Target.Outputs:
                # Only do build for target with outputs
                self._FinalBuildTargetList.add(Target)

            self._BuildTargets[FileType].add(Target)

            if not Source.IsBinary and Source == File:
                self._IntroBuildTargetList.add(Target)

            # to avoid cyclic rule
            if FileType in RuleChain:
                break

            RuleChain.add(FileType)
            SourceList.extend(Target.Outputs)
            LastTarget = Target
            FileType = TAB_UNKNOWN_FILE

    @cached_property
    def Targets(self):
        if self._BuildTargets is None:
            self._IntroBuildTargetList = set()
            self._FinalBuildTargetList = set()
            self._BuildTargets = defaultdict(set)
            self._FileTypes = defaultdict(set)

        #TRICK: call SourceFileList property to apply build rule for source files
        self.SourceFileList

        #TRICK: call _GetBinaryFileList to apply build rule for binary files
        self.BinaryFileList

        return self._BuildTargets

    @cached_property
    def IntroTargetList(self):
        self.Targets
        return self._IntroBuildTargetList

    @cached_property
    def CodaTargetList(self):
        self.Targets
        return self._FinalBuildTargetList

    @cached_property
    def FileTypes(self):
        self.Targets
        return self._FileTypes

    ## Get the list of package object the module depends on
    #
    #   @retval     list    The package object list
    #
    @cached_property
    def DependentPackageList(self):
        return self.Module.Packages

    ## Return the list of auto-generated code file
    #
    #   @retval     list        The list of auto-generated file
    #
    @cached_property
    def AutoGenFileList(self):
        AutoGenUniIdf = self.BuildType != 'UEFI_HII'
        UniStringBinBuffer = BytesIO()
        IdfGenBinBuffer = BytesIO()
        RetVal = {}
        AutoGenC = TemplateString()
        AutoGenH = TemplateString()
        StringH = TemplateString()
        StringIdf = TemplateString()
        GenC.CreateCode(self, AutoGenC, AutoGenH, StringH, AutoGenUniIdf, UniStringBinBuffer, StringIdf, AutoGenUniIdf, IdfGenBinBuffer)
        #
        # AutoGen.c is generated if there are library classes in inf, or there are object files
        #
        if str(AutoGenC) != "" and (len(self.Module.LibraryClasses) > 0
                                    or TAB_OBJECT_FILE in self.FileTypes):
            AutoFile = PathClass(gAutoGenCodeFileName, self.DebugDir)
            RetVal[AutoFile] = str(AutoGenC)
            self._ApplyBuildRule(AutoFile, TAB_UNKNOWN_FILE)
        if str(AutoGenH) != "":
            AutoFile = PathClass(gAutoGenHeaderFileName, self.DebugDir)
            RetVal[AutoFile] = str(AutoGenH)
            self._ApplyBuildRule(AutoFile, TAB_UNKNOWN_FILE)
        if str(StringH) != "":
            AutoFile = PathClass(gAutoGenStringFileName % {"module_name":self.Name}, self.DebugDir)
            RetVal[AutoFile] = str(StringH)
            self._ApplyBuildRule(AutoFile, TAB_UNKNOWN_FILE)
        if UniStringBinBuffer is not None and UniStringBinBuffer.getvalue() != b"":
            AutoFile = PathClass(gAutoGenStringFormFileName % {"module_name":self.Name}, self.OutputDir)
            RetVal[AutoFile] = UniStringBinBuffer.getvalue()
            AutoFile.IsBinary = True
            self._ApplyBuildRule(AutoFile, TAB_UNKNOWN_FILE)
        if UniStringBinBuffer is not None:
            UniStringBinBuffer.close()
        if str(StringIdf) != "":
            AutoFile = PathClass(gAutoGenImageDefFileName % {"module_name":self.Name}, self.DebugDir)
            RetVal[AutoFile] = str(StringIdf)
            self._ApplyBuildRule(AutoFile, TAB_UNKNOWN_FILE)
        if IdfGenBinBuffer is not None and IdfGenBinBuffer.getvalue() != b"":
            AutoFile = PathClass(gAutoGenIdfFileName % {"module_name":self.Name}, self.OutputDir)
            RetVal[AutoFile] = IdfGenBinBuffer.getvalue()
            AutoFile.IsBinary = True
            self._ApplyBuildRule(AutoFile, TAB_UNKNOWN_FILE)
        if IdfGenBinBuffer is not None:
            IdfGenBinBuffer.close()
        return RetVal

    ## Return the list of library modules explicitly or implicitly used by this module
    @cached_property
    def DependentLibraryList(self):
        # only merge library classes and PCD for non-library module
        if self.IsLibrary:
            return []
        return self.PlatformInfo.ApplyLibraryInstance(self.Module)

    ## Get the list of PCDs from current module
    #
    #   @retval     list                    The list of PCD
    #
    @cached_property
    def ModulePcdList(self):
        # apply PCD settings from platform
        RetVal = self.PlatformInfo.ApplyPcdSetting(self.Module, self.Module.Pcds)

        return RetVal
    @cached_property
    def _PcdComments(self):
        ReVal = OrderedListDict()
        ExtendCopyDictionaryLists(ReVal, self.Module.PcdComments)
        if not self.IsLibrary:
            for Library in self.DependentLibraryList:
                ExtendCopyDictionaryLists(ReVal, Library.PcdComments)
        return ReVal

    ## Get the list of PCDs from dependent libraries
    #
    #   @retval     list                    The list of PCD
    #
    @cached_property
    def LibraryPcdList(self):
        if self.IsLibrary:
            return []
        RetVal = []
        Pcds = set()
        # get PCDs from dependent libraries
        for Library in self.DependentLibraryList:
            PcdsInLibrary = OrderedDict()
            for Key in Library.Pcds:
                # skip duplicated PCDs
                if Key in self.Module.Pcds or Key in Pcds:
                    continue
                Pcds.add(Key)
                PcdsInLibrary[Key] = copy.copy(Library.Pcds[Key])
            RetVal.extend(self.PlatformInfo.ApplyPcdSetting(self.Module, PcdsInLibrary, Library=Library))
        return RetVal

    ## Get the GUID value mapping
    #
    #   @retval     dict    The mapping between GUID cname and its value
    #
    @cached_property
    def GuidList(self):
        RetVal = self.Module.Guids
        for Library in self.DependentLibraryList:
            RetVal.update(Library.Guids)
            ExtendCopyDictionaryLists(self._GuidComments, Library.GuidComments)
        ExtendCopyDictionaryLists(self._GuidComments, self.Module.GuidComments)
        return RetVal

    @cached_property
    def GetGuidsUsedByPcd(self):
        RetVal = OrderedDict(self.Module.GetGuidsUsedByPcd())
        for Library in self.DependentLibraryList:
            RetVal.update(Library.GetGuidsUsedByPcd())
        return RetVal
    ## Get the protocol value mapping
    #
    #   @retval     dict    The mapping between protocol cname and its value
    #
    @cached_property
    def ProtocolList(self):
        RetVal = OrderedDict(self.Module.Protocols)
        for Library in self.DependentLibraryList:
            RetVal.update(Library.Protocols)
            ExtendCopyDictionaryLists(self._ProtocolComments, Library.ProtocolComments)
        ExtendCopyDictionaryLists(self._ProtocolComments, self.Module.ProtocolComments)
        return RetVal

    ## Get the PPI value mapping
    #
    #   @retval     dict    The mapping between PPI cname and its value
    #
    @cached_property
    def PpiList(self):
        RetVal = OrderedDict(self.Module.Ppis)
        for Library in self.DependentLibraryList:
            RetVal.update(Library.Ppis)
            ExtendCopyDictionaryLists(self._PpiComments, Library.PpiComments)
        ExtendCopyDictionaryLists(self._PpiComments, self.Module.PpiComments)
        return RetVal

    ## Get the list of include search path
    #
    #   @retval     list                    The list path
    #
    @cached_property
    def IncludePathList(self):
        RetVal = []
        RetVal.append(self.MetaFile.Dir)
        RetVal.append(self.DebugDir)

        for Package in self.Module.Packages:
            PackageDir = mws.join(self.WorkspaceDir, Package.MetaFile.Dir)
            if PackageDir not in RetVal:
                RetVal.append(PackageDir)
            IncludesList = Package.Includes
            if Package._PrivateIncludes:
                if not self.MetaFile.OriginalPath.Path.startswith(PackageDir):
                    IncludesList = list(set(Package.Includes).difference(set(Package._PrivateIncludes)))
            for Inc in IncludesList:
                if Inc not in RetVal:
                    RetVal.append(str(Inc))
        return RetVal

    @cached_property
    def IncludePathLength(self):
        return sum(len(inc)+1 for inc in self.IncludePathList)

    ## Get the list of include paths from the packages
    #
    #   @IncludesList     list             The list path
    #
    @cached_property
    def PackageIncludePathList(self):
        IncludesList = []
        for Package in self.Module.Packages:
            PackageDir = mws.join(self.WorkspaceDir, Package.MetaFile.Dir)
            IncludesList = Package.Includes
            if Package._PrivateIncludes:
                if not self.MetaFile.Path.startswith(PackageDir):
                    IncludesList = list(set(Package.Includes).difference(set(Package._PrivateIncludes)))
        return IncludesList

    ## Get HII EX PCDs which maybe used by VFR
    #
    #  efivarstore used by VFR may relate with HII EX PCDs
    #  Get the variable name and GUID from efivarstore and HII EX PCD
    #  List the HII EX PCDs in As Built INF if both name and GUID match.
    #
    #  @retval    list    HII EX PCDs
    #
    def _GetPcdsMaybeUsedByVfr(self):
        if not self.SourceFileList:
            return []

        NameGuids = set()
        for SrcFile in self.SourceFileList:
            if SrcFile.Ext.lower() != '.vfr':
                continue
            Vfri = os.path.join(self.OutputDir, SrcFile.BaseName + '.i')
            if not os.path.exists(Vfri):
                continue
            VfriFile = open(Vfri, 'r')
            Content = VfriFile.read()
            VfriFile.close()
            Pos = Content.find('efivarstore')
            while Pos != -1:
                #
                # Make sure 'efivarstore' is the start of efivarstore statement
                # In case of the value of 'name' (name = efivarstore) is equal to 'efivarstore'
                #
                Index = Pos - 1
                while Index >= 0 and Content[Index] in ' \t\r\n':
                    Index -= 1
                if Index >= 0 and Content[Index] != ';':
                    Pos = Content.find('efivarstore', Pos + len('efivarstore'))
                    continue
                #
                # 'efivarstore' must be followed by name and guid
                #
                Name = gEfiVarStoreNamePattern.search(Content, Pos)
                if not Name:
                    break
                Guid = gEfiVarStoreGuidPattern.search(Content, Pos)
                if not Guid:
                    break
                NameArray = _ConvertStringToByteArray('L"' + Name.group(1) + '"')
                NameGuids.add((NameArray, GuidStructureStringToGuidString(Guid.group(1))))
                Pos = Content.find('efivarstore', Name.end())
        if not NameGuids:
            return []
        HiiExPcds = []
        for Pcd in self.PlatformInfo.Pcds.values():
            if Pcd.Type != TAB_PCDS_DYNAMIC_EX_HII:
                continue
            for SkuInfo in Pcd.SkuInfoList.values():
                Value = GuidValue(SkuInfo.VariableGuid, self.PlatformInfo.PackageList, self.MetaFile.Path)
                if not Value:
                    continue
                Name = _ConvertStringToByteArray(SkuInfo.VariableName)
                Guid = GuidStructureStringToGuidString(Value)
                if (Name, Guid) in NameGuids and Pcd not in HiiExPcds:
                    HiiExPcds.append(Pcd)
                    break

        return HiiExPcds

    def _GenOffsetBin(self):
        VfrUniBaseName = {}
        for SourceFile in self.Module.Sources:
            if SourceFile.Type.upper() == ".VFR" :
                #
                # search the .map file to find the offset of vfr binary in the PE32+/TE file.
                #
                VfrUniBaseName[SourceFile.BaseName] = (SourceFile.BaseName + "Bin")
            elif SourceFile.Type.upper() == ".UNI" :
                #
                # search the .map file to find the offset of Uni strings binary in the PE32+/TE file.
                #
                VfrUniBaseName["UniOffsetName"] = (self.Name + "Strings")

        if not VfrUniBaseName:
            return None
        MapFileName = os.path.join(self.OutputDir, self.Name + ".map")
        EfiFileName = os.path.join(self.OutputDir, self.Name + ".efi")
        VfrUniOffsetList = GetVariableOffset(MapFileName, EfiFileName, list(VfrUniBaseName.values()))
        if not VfrUniOffsetList:
            return None

        OutputName = '%sOffset.bin' % self.Name
        UniVfrOffsetFileName    =  os.path.join( self.OutputDir, OutputName)

        try:
            fInputfile = open(UniVfrOffsetFileName, "wb+", 0)
        except:
            EdkLogger.error("build", FILE_OPEN_FAILURE, "File open failed for %s" % UniVfrOffsetFileName, None)

        # Use a instance of BytesIO to cache data
        fStringIO = BytesIO()

        for Item in VfrUniOffsetList:
            if (Item[0].find("Strings") != -1):
                #
                # UNI offset in image.
                # GUID + Offset
                # { 0x8913c5e0, 0x33f6, 0x4d86, { 0x9b, 0xf1, 0x43, 0xef, 0x89, 0xfc, 0x6, 0x66 } }
                #
                UniGuid = b'\xe0\xc5\x13\x89\xf63\x86M\x9b\xf1C\xef\x89\xfc\x06f'
                fStringIO.write(UniGuid)
                UniValue = pack ('Q', int (Item[1], 16))
                fStringIO.write (UniValue)
            else:
                #
                # VFR binary offset in image.
                # GUID + Offset
                # { 0xd0bc7cb4, 0x6a47, 0x495f, { 0xaa, 0x11, 0x71, 0x7, 0x46, 0xda, 0x6, 0xa2 } };
                #
                VfrGuid = b'\xb4|\xbc\xd0Gj_I\xaa\x11q\x07F\xda\x06\xa2'
                fStringIO.write(VfrGuid)
                VfrValue = pack ('Q', int (Item[1], 16))
                fStringIO.write (VfrValue)
        #
        # write data into file.
        #
        try :
            fInputfile.write (fStringIO.getvalue())
        except:
            EdkLogger.error("build", FILE_WRITE_FAILURE, "Write data to file %s failed, please check whether the "
                            "file been locked or using by other applications." %UniVfrOffsetFileName, None)

        fStringIO.close ()
        fInputfile.close ()
        return OutputName
    @cached_property
    def OutputFile(self):
        retVal = set()
        OutputDir = self.OutputDir.replace('\\', '/').strip('/')
        DebugDir = self.DebugDir.replace('\\', '/').strip('/')
        for Item in self.CodaTargetList:
            File = Item.Target.Path.replace('\\', '/').strip('/').replace(DebugDir, '').replace(OutputDir, '').strip('/')
            retVal.add(File)
        if self.DepexGenerated:
            retVal.add(self.Name + '.depex')

        Bin = self._GenOffsetBin()
        if Bin:
            retVal.add(Bin)

        for Root, Dirs, Files in os.walk(OutputDir):
            for File in Files:
                if File.lower().endswith('.pdb'):
                    retVal.add(File)

        return retVal

    ## Create AsBuilt INF file the module
    #
    def CreateAsBuiltInf(self):

        if self.IsAsBuiltInfCreated:
            return

        # Skip INF file generation for libraries
        if self.IsLibrary:
            return

        # Skip the following code for modules with no source files
        if not self.SourceFileList:
            return

        # Skip the following code for modules without any binary files
        if self.BinaryFileList:
            return

        ### TODO: How to handles mixed source and binary modules

        # Find all DynamicEx and PatchableInModule PCDs used by this module and dependent libraries
        # Also find all packages that the DynamicEx PCDs depend on
        Pcds = []
        PatchablePcds = []
        Packages = []
        PcdCheckList = []
        PcdTokenSpaceList = []
        for Pcd in self.ModulePcdList + self.LibraryPcdList:
            if Pcd.Type == TAB_PCDS_PATCHABLE_IN_MODULE:
                PatchablePcds.append(Pcd)
                PcdCheckList.append((Pcd.TokenCName, Pcd.TokenSpaceGuidCName, TAB_PCDS_PATCHABLE_IN_MODULE))
            elif Pcd.Type in PCD_DYNAMIC_EX_TYPE_SET:
                if Pcd not in Pcds:
                    Pcds.append(Pcd)
                    PcdCheckList.append((Pcd.TokenCName, Pcd.TokenSpaceGuidCName, TAB_PCDS_DYNAMIC_EX))
                    PcdCheckList.append((Pcd.TokenCName, Pcd.TokenSpaceGuidCName, TAB_PCDS_DYNAMIC))
                    PcdTokenSpaceList.append(Pcd.TokenSpaceGuidCName)
        GuidList = OrderedDict(self.GuidList)
        for TokenSpace in self.GetGuidsUsedByPcd:
            # If token space is not referred by patch PCD or Ex PCD, remove the GUID from GUID list
            # The GUIDs in GUIDs section should really be the GUIDs in source INF or referred by Ex an patch PCDs
            if TokenSpace not in PcdTokenSpaceList and TokenSpace in GuidList:
                GuidList.pop(TokenSpace)
        CheckList = (GuidList, self.PpiList, self.ProtocolList, PcdCheckList)
        for Package in self.DerivedPackageList:
            if Package in Packages:
                continue
            BeChecked = (Package.Guids, Package.Ppis, Package.Protocols, Package.Pcds)
            Found = False
            for Index in range(len(BeChecked)):
                for Item in CheckList[Index]:
                    if Item in BeChecked[Index]:
                        Packages.append(Package)
                        Found = True
                        break
                if Found:
                    break

        VfrPcds = self._GetPcdsMaybeUsedByVfr()
        for Pkg in self.PlatformInfo.PackageList:
            if Pkg in Packages:
                continue
            for VfrPcd in VfrPcds:
                if ((VfrPcd.TokenCName, VfrPcd.TokenSpaceGuidCName, TAB_PCDS_DYNAMIC_EX) in Pkg.Pcds or
                    (VfrPcd.TokenCName, VfrPcd.TokenSpaceGuidCName, TAB_PCDS_DYNAMIC) in Pkg.Pcds):
                    Packages.append(Pkg)
                    break

        ModuleType = SUP_MODULE_DXE_DRIVER if self.ModuleType == SUP_MODULE_UEFI_DRIVER and self.DepexGenerated else self.ModuleType
        DriverType = self.PcdIsDriver if self.PcdIsDriver else ''
        Guid = self.Guid
        MDefs = self.Module.Defines

        AsBuiltInfDict = {
          'module_name'                       : self.Name,
          'module_guid'                       : Guid,
          'module_module_type'                : ModuleType,
          'module_version_string'             : [MDefs['VERSION_STRING']] if 'VERSION_STRING' in MDefs else [],
          'pcd_is_driver_string'              : [],
          'module_uefi_specification_version' : [],
          'module_pi_specification_version'   : [],
          'module_entry_point'                : self.Module.ModuleEntryPointList,
          'module_unload_image'               : self.Module.ModuleUnloadImageList,
          'module_constructor'                : self.Module.ConstructorList,
          'module_destructor'                 : self.Module.DestructorList,
          'module_shadow'                     : [MDefs['SHADOW']] if 'SHADOW' in MDefs else [],
          'module_pci_vendor_id'              : [MDefs['PCI_VENDOR_ID']] if 'PCI_VENDOR_ID' in MDefs else [],
          'module_pci_device_id'              : [MDefs['PCI_DEVICE_ID']] if 'PCI_DEVICE_ID' in MDefs else [],
          'module_pci_class_code'             : [MDefs['PCI_CLASS_CODE']] if 'PCI_CLASS_CODE' in MDefs else [],
          'module_pci_revision'               : [MDefs['PCI_REVISION']] if 'PCI_REVISION' in MDefs else [],
          'module_build_number'               : [MDefs['BUILD_NUMBER']] if 'BUILD_NUMBER' in MDefs else [],
          'module_spec'                       : [MDefs['SPEC']] if 'SPEC' in MDefs else [],
          'module_uefi_hii_resource_section'  : [MDefs['UEFI_HII_RESOURCE_SECTION']] if 'UEFI_HII_RESOURCE_SECTION' in MDefs else [],
          'module_uni_file'                   : [MDefs['MODULE_UNI_FILE']] if 'MODULE_UNI_FILE' in MDefs else [],
          'module_arch'                       : self.Arch,
          'package_item'                      : [Package.MetaFile.File.replace('\\', '/') for Package in Packages],
          'binary_item'                       : [],
          'patchablepcd_item'                 : [],
          'pcd_item'                          : [],
          'protocol_item'                     : [],
          'ppi_item'                          : [],
          'guid_item'                         : [],
          'flags_item'                        : [],
          'libraryclasses_item'               : []
        }

        if 'MODULE_UNI_FILE' in MDefs:
            UNIFile = os.path.join(self.MetaFile.Dir, MDefs['MODULE_UNI_FILE'])
            if os.path.isfile(UNIFile):
                shutil.copy2(UNIFile, self.OutputDir)

        if self.AutoGenVersion > int(gInfSpecVersion, 0):
            AsBuiltInfDict['module_inf_version'] = '0x%08x' % self.AutoGenVersion
        else:
            AsBuiltInfDict['module_inf_version'] = gInfSpecVersion

        if DriverType:
            AsBuiltInfDict['pcd_is_driver_string'].append(DriverType)

        if 'UEFI_SPECIFICATION_VERSION' in self.Specification:
            AsBuiltInfDict['module_uefi_specification_version'].append(self.Specification['UEFI_SPECIFICATION_VERSION'])
        if 'PI_SPECIFICATION_VERSION' in self.Specification:
            AsBuiltInfDict['module_pi_specification_version'].append(self.Specification['PI_SPECIFICATION_VERSION'])

        OutputDir = self.OutputDir.replace('\\', '/').strip('/')
        DebugDir = self.DebugDir.replace('\\', '/').strip('/')
        for Item in self.CodaTargetList:
            File = Item.Target.Path.replace('\\', '/').strip('/').replace(DebugDir, '').replace(OutputDir, '').strip('/')
            if os.path.isabs(File):
                File = File.replace('\\', '/').strip('/').replace(OutputDir, '').strip('/')
            if Item.Target.Ext.lower() == '.aml':
                AsBuiltInfDict['binary_item'].append('ASL|' + File)
            elif Item.Target.Ext.lower() == '.acpi':
                AsBuiltInfDict['binary_item'].append('ACPI|' + File)
            elif Item.Target.Ext.lower() == '.efi':
                AsBuiltInfDict['binary_item'].append('PE32|' + self.Name + '.efi')
            else:
                AsBuiltInfDict['binary_item'].append('BIN|' + File)
        if not self.DepexGenerated:
            DepexFile = os.path.join(self.OutputDir, self.Name + '.depex')
            if os.path.exists(DepexFile):
                self.DepexGenerated = True
        if self.DepexGenerated:
            if self.ModuleType in [SUP_MODULE_PEIM]:
                AsBuiltInfDict['binary_item'].append('PEI_DEPEX|' + self.Name + '.depex')
            elif self.ModuleType in [SUP_MODULE_DXE_DRIVER, SUP_MODULE_DXE_RUNTIME_DRIVER, SUP_MODULE_DXE_SAL_DRIVER, SUP_MODULE_UEFI_DRIVER]:
                AsBuiltInfDict['binary_item'].append('DXE_DEPEX|' + self.Name + '.depex')
            elif self.ModuleType in [SUP_MODULE_DXE_SMM_DRIVER]:
                AsBuiltInfDict['binary_item'].append('SMM_DEPEX|' + self.Name + '.depex')

        Bin = self._GenOffsetBin()
        if Bin:
            AsBuiltInfDict['binary_item'].append('BIN|%s' % Bin)

        for Root, Dirs, Files in os.walk(OutputDir):
            for File in Files:
                if File.lower().endswith('.pdb'):
                    AsBuiltInfDict['binary_item'].append('DISPOSABLE|' + File)
        HeaderComments = self.Module.HeaderComments
        StartPos = 0
        for Index in range(len(HeaderComments)):
            if HeaderComments[Index].find('@BinaryHeader') != -1:
                HeaderComments[Index] = HeaderComments[Index].replace('@BinaryHeader', '@file')
                StartPos = Index
                break
        AsBuiltInfDict['header_comments'] = '\n'.join(HeaderComments[StartPos:]).replace(':#', '://')
        AsBuiltInfDict['tail_comments'] = '\n'.join(self.Module.TailComments)

        GenList = [
            (self.ProtocolList, self._ProtocolComments, 'protocol_item'),
            (self.PpiList, self._PpiComments, 'ppi_item'),
            (GuidList, self._GuidComments, 'guid_item')
        ]
        for Item in GenList:
            for CName in Item[0]:
                Comments = '\n  '.join(Item[1][CName]) if CName in Item[1] else ''
                Entry = Comments + '\n  ' + CName if Comments else CName
                AsBuiltInfDict[Item[2]].append(Entry)
        PatchList = parsePcdInfoFromMapFile(
                            os.path.join(self.OutputDir, self.Name + '.map'),
                            os.path.join(self.OutputDir, self.Name + '.efi')
                        )
        if PatchList:
            for Pcd in PatchablePcds:
                TokenCName = Pcd.TokenCName
                for PcdItem in GlobalData.MixedPcd:
                    if (Pcd.TokenCName, Pcd.TokenSpaceGuidCName) in GlobalData.MixedPcd[PcdItem]:
                        TokenCName = PcdItem[0]
                        break
                for PatchPcd in PatchList:
                    if TokenCName == PatchPcd[0]:
                        break
                else:
                    continue
                PcdValue = ''
                if Pcd.DatumType == 'BOOLEAN':
                    BoolValue = Pcd.DefaultValue.upper()
                    if BoolValue == 'TRUE':
                        Pcd.DefaultValue = '1'
                    elif BoolValue == 'FALSE':
                        Pcd.DefaultValue = '0'

                if Pcd.DatumType in TAB_PCD_NUMERIC_TYPES:
                    HexFormat = '0x%02x'
                    if Pcd.DatumType == TAB_UINT16:
                        HexFormat = '0x%04x'
                    elif Pcd.DatumType == TAB_UINT32:
                        HexFormat = '0x%08x'
                    elif Pcd.DatumType == TAB_UINT64:
                        HexFormat = '0x%016x'
                    PcdValue = HexFormat % int(Pcd.DefaultValue, 0)
                else:
                    if Pcd.MaxDatumSize is None or Pcd.MaxDatumSize == '':
                        EdkLogger.error("build", AUTOGEN_ERROR,
                                        "Unknown [MaxDatumSize] of PCD [%s.%s]" % (Pcd.TokenSpaceGuidCName, TokenCName)
                                        )
                    ArraySize = int(Pcd.MaxDatumSize, 0)
                    PcdValue = Pcd.DefaultValue
                    if PcdValue[0] != '{':
                        Unicode = False
                        if PcdValue[0] == 'L':
                            Unicode = True
                        PcdValue = PcdValue.lstrip('L')
                        PcdValue = eval(PcdValue)
                        NewValue = '{'
                        for Index in range(0, len(PcdValue)):
                            if Unicode:
                                CharVal = ord(PcdValue[Index])
                                NewValue = NewValue + '0x%02x' % (CharVal & 0x00FF) + ', ' \
                                        + '0x%02x' % (CharVal >> 8) + ', '
                            else:
                                NewValue = NewValue + '0x%02x' % (ord(PcdValue[Index]) % 0x100) + ', '
                        Padding = '0x00, '
                        if Unicode:
                            Padding = Padding * 2
                            ArraySize = ArraySize // 2
                        if ArraySize < (len(PcdValue) + 1):
                            if Pcd.MaxSizeUserSet:
                                EdkLogger.error("build", AUTOGEN_ERROR,
                                            "The maximum size of VOID* type PCD '%s.%s' is less than its actual size occupied." % (Pcd.TokenSpaceGuidCName, TokenCName)
                                            )
                            else:
                                ArraySize = len(PcdValue) + 1
                        if ArraySize > len(PcdValue) + 1:
                            NewValue = NewValue + Padding * (ArraySize - len(PcdValue) - 1)
                        PcdValue = NewValue + Padding.strip().rstrip(',') + '}'
                    elif len(PcdValue.split(',')) <= ArraySize:
                        PcdValue = PcdValue.rstrip('}') + ', 0x00' * (ArraySize - len(PcdValue.split(',')))
                        PcdValue += '}'
                    else:
                        if Pcd.MaxSizeUserSet:
                            EdkLogger.error("build", AUTOGEN_ERROR,
                                        "The maximum size of VOID* type PCD '%s.%s' is less than its actual size occupied." % (Pcd.TokenSpaceGuidCName, TokenCName)
                                        )
                        else:
                            ArraySize = len(PcdValue) + 1
                PcdItem = '%s.%s|%s|0x%X' % \
                    (Pcd.TokenSpaceGuidCName, TokenCName, PcdValue, PatchPcd[1])
                PcdComments = ''
                if (Pcd.TokenSpaceGuidCName, Pcd.TokenCName) in self._PcdComments:
                    PcdComments = '\n  '.join(self._PcdComments[Pcd.TokenSpaceGuidCName, Pcd.TokenCName])
                if PcdComments:
                    PcdItem = PcdComments + '\n  ' + PcdItem
                AsBuiltInfDict['patchablepcd_item'].append(PcdItem)

        for Pcd in Pcds + VfrPcds:
            PcdCommentList = []
            HiiInfo = ''
            TokenCName = Pcd.TokenCName
            for PcdItem in GlobalData.MixedPcd:
                if (Pcd.TokenCName, Pcd.TokenSpaceGuidCName) in GlobalData.MixedPcd[PcdItem]:
                    TokenCName = PcdItem[0]
                    break
            if Pcd.Type == TAB_PCDS_DYNAMIC_EX_HII:
                for SkuName in Pcd.SkuInfoList:
                    SkuInfo = Pcd.SkuInfoList[SkuName]
                    HiiInfo = '## %s|%s|%s' % (SkuInfo.VariableName, SkuInfo.VariableGuid, SkuInfo.VariableOffset)
                    break
            if (Pcd.TokenSpaceGuidCName, Pcd.TokenCName) in self._PcdComments:
                PcdCommentList = self._PcdComments[Pcd.TokenSpaceGuidCName, Pcd.TokenCName][:]
            if HiiInfo:
                UsageIndex = -1
                UsageStr = ''
                for Index, Comment in enumerate(PcdCommentList):
                    for Usage in UsageList:
                        if Comment.find(Usage) != -1:
                            UsageStr = Usage
                            UsageIndex = Index
                            break
                if UsageIndex != -1:
                    PcdCommentList[UsageIndex] = '## %s %s %s' % (UsageStr, HiiInfo, PcdCommentList[UsageIndex].replace(UsageStr, ''))
                else:
                    PcdCommentList.append('## UNDEFINED ' + HiiInfo)
            PcdComments = '\n  '.join(PcdCommentList)
            PcdEntry = Pcd.TokenSpaceGuidCName + '.' + TokenCName
            if PcdComments:
                PcdEntry = PcdComments + '\n  ' + PcdEntry
            AsBuiltInfDict['pcd_item'].append(PcdEntry)
        for Item in self.BuildOption:
            if 'FLAGS' in self.BuildOption[Item]:
                AsBuiltInfDict['flags_item'].append('%s:%s_%s_%s_%s_FLAGS = %s' % (self.ToolChainFamily, self.BuildTarget, self.ToolChain, self.Arch, Item, self.BuildOption[Item]['FLAGS'].strip()))

        # Generated LibraryClasses section in comments.
        for Library in self.LibraryAutoGenList:
            AsBuiltInfDict['libraryclasses_item'].append(Library.MetaFile.File.replace('\\', '/'))

        # Generated UserExtensions TianoCore section.
        # All tianocore user extensions are copied.
        UserExtStr = ''
        for TianoCore in self._GetTianoCoreUserExtensionList():
            UserExtStr += '\n'.join(TianoCore)
            ExtensionFile = os.path.join(self.MetaFile.Dir, TianoCore[1])
            if os.path.isfile(ExtensionFile):
                shutil.copy2(ExtensionFile, self.OutputDir)
        AsBuiltInfDict['userextension_tianocore_item'] = UserExtStr

        # Generated depex expression section in comments.
        DepexExpression = self._GetDepexExpresionString()
        AsBuiltInfDict['depexsection_item'] = DepexExpression if DepexExpression else ''

        AsBuiltInf = TemplateString()
        AsBuiltInf.Append(gAsBuiltInfHeaderString.Replace(AsBuiltInfDict))

        SaveFileOnChange(os.path.join(self.OutputDir, self.Name + '.inf'), str(AsBuiltInf), False)

        self.IsAsBuiltInfCreated = True

    def CopyModuleToCache(self):
        FileDir = path.join(GlobalData.gBinCacheDest, self.PlatformInfo.Name, self.BuildTarget + "_" + self.ToolChain, self.Arch, self.SourceDir, self.MetaFile.BaseName)
        CreateDirectory (FileDir)
        HashFile = path.join(self.BuildDir, self.Name + '.hash')
        if os.path.exists(HashFile):
            CopyFileOnChange(HashFile, FileDir)
        ModuleFile = path.join(self.OutputDir, self.Name + '.inf')
        if os.path.exists(ModuleFile):
            CopyFileOnChange(ModuleFile, FileDir)
        if not self.OutputFile:
            Ma = self.BuildDatabase[self.MetaFile, self.Arch, self.BuildTarget, self.ToolChain]
            self.OutputFile = Ma.Binaries
        for File in self.OutputFile:
            File = str(File)
            if not os.path.isabs(File):
                File = os.path.join(self.OutputDir, File)
            if os.path.exists(File):
                sub_dir = os.path.relpath(File, self.OutputDir)
                destination_file = os.path.join(FileDir, sub_dir)
                destination_dir = os.path.dirname(destination_file)
                CreateDirectory(destination_dir)
                CopyFileOnChange(File, destination_dir)

    def AttemptModuleCacheCopy(self):
        # If library or Module is binary do not skip by hash
        if self.IsBinaryModule:
            return False
        # .inc is contains binary information so do not skip by hash as well
        for f_ext in self.SourceFileList:
            if '.inc' in str(f_ext):
                return False
        FileDir = path.join(GlobalData.gBinCacheSource, self.PlatformInfo.Name, self.BuildTarget + "_" + self.ToolChain, self.Arch, self.SourceDir, self.MetaFile.BaseName)
        HashFile = path.join(FileDir, self.Name + '.hash')
        if os.path.exists(HashFile):
            f = open(HashFile, 'r')
            CacheHash = f.read()
            f.close()
            self.GenModuleHash()
            if GlobalData.gModuleHash[self.Arch][self.Name]:
                if CacheHash == GlobalData.gModuleHash[self.Arch][self.Name]:
                    for root, dir, files in os.walk(FileDir):
                        for f in files:
                            if self.Name + '.hash' in f:
                                CopyFileOnChange(HashFile, self.BuildDir)
                            else:
                                File = path.join(root, f)
                                sub_dir = os.path.relpath(File, FileDir)
                                destination_file = os.path.join(self.OutputDir, sub_dir)
                                destination_dir = os.path.dirname(destination_file)
                                CreateDirectory(destination_dir)
                                CopyFileOnChange(File, destination_dir)
                    if self.Name == "PcdPeim" or self.Name == "PcdDxe":
                        CreatePcdDatabaseCode(self, TemplateString(), TemplateString())
                    return True
        return False

    ## Create makefile for the module and its dependent libraries
    #
    #   @param      CreateLibraryMakeFile   Flag indicating if or not the makefiles of
    #                                       dependent libraries will be created
    #
    @cached_class_function
    def CreateMakeFile(self, CreateLibraryMakeFile=True, GenFfsList = []):
        # nest this function inside it's only caller.
        def CreateTimeStamp():
            FileSet = {self.MetaFile.Path}

            for SourceFile in self.Module.Sources:
                FileSet.add (SourceFile.Path)

            for Lib in self.DependentLibraryList:
                FileSet.add (Lib.MetaFile.Path)

            for f in self.AutoGenDepSet:
                FileSet.add (f.Path)

            if os.path.exists (self.TimeStampPath):
                os.remove (self.TimeStampPath)
            with open(self.TimeStampPath, 'w+') as fd:
                for f in FileSet:
                    fd.write(f)
                    fd.write("\n")

        # Ignore generating makefile when it is a binary module
        if self.IsBinaryModule:
            return

        self.GenFfsList = GenFfsList

        if not self.IsLibrary and CreateLibraryMakeFile:
            for LibraryAutoGen in self.LibraryAutoGenList:
                LibraryAutoGen.CreateMakeFile()

        # Don't enable if hash feature enabled, CanSkip uses timestamps to determine build skipping
        if not GlobalData.gUseHashCache and self.CanSkip():
            return

        if len(self.CustomMakefile) == 0:
            Makefile = GenMake.ModuleMakefile(self)
        else:
            Makefile = GenMake.CustomMakefile(self)
        if Makefile.Generate():
            EdkLogger.debug(EdkLogger.DEBUG_9, "Generated makefile for module %s [%s]" %
                            (self.Name, self.Arch))
        else:
            EdkLogger.debug(EdkLogger.DEBUG_9, "Skipped the generation of makefile for module %s [%s]" %
                            (self.Name, self.Arch))

        CreateTimeStamp()

    def CopyBinaryFiles(self):
        for File in self.Module.Binaries:
            SrcPath = File.Path
            DstPath = os.path.join(self.OutputDir, os.path.basename(SrcPath))
            CopyLongFilePath(SrcPath, DstPath)
    ## Create autogen code for the module and its dependent libraries
    #
    #   @param      CreateLibraryCodeFile   Flag indicating if or not the code of
    #                                       dependent libraries will be created
    #
    def CreateCodeFile(self, CreateLibraryCodeFile=True):
        if self.IsCodeFileCreated:
            return

        # Need to generate PcdDatabase even PcdDriver is binarymodule
        if self.IsBinaryModule and self.PcdIsDriver != '':
            CreatePcdDatabaseCode(self, TemplateString(), TemplateString())
            return
        if self.IsBinaryModule:
            if self.IsLibrary:
                self.CopyBinaryFiles()
            return

        if not self.IsLibrary and CreateLibraryCodeFile:
            for LibraryAutoGen in self.LibraryAutoGenList:
                LibraryAutoGen.CreateCodeFile()
        # Don't enable if hash feature enabled, CanSkip uses timestamps to determine build skipping
        if not GlobalData.gUseHashCache and self.CanSkip():
            return

        AutoGenList = []
        IgoredAutoGenList = []

        for File in self.AutoGenFileList:
            if GenC.Generate(File.Path, self.AutoGenFileList[File], File.IsBinary):
                AutoGenList.append(str(File))
            else:
                IgoredAutoGenList.append(str(File))


        for ModuleType in self.DepexList:
            # Ignore empty [depex] section or [depex] section for SUP_MODULE_USER_DEFINED module
            if len(self.DepexList[ModuleType]) == 0 or ModuleType == SUP_MODULE_USER_DEFINED or ModuleType == SUP_MODULE_HOST_APPLICATION:
                continue

            Dpx = GenDepex.DependencyExpression(self.DepexList[ModuleType], ModuleType, True)
            DpxFile = gAutoGenDepexFileName % {"module_name" : self.Name}

            if len(Dpx.PostfixNotation) != 0:
                self.DepexGenerated = True

            if Dpx.Generate(path.join(self.OutputDir, DpxFile)):
                AutoGenList.append(str(DpxFile))
            else:
                IgoredAutoGenList.append(str(DpxFile))

        if IgoredAutoGenList == []:
            EdkLogger.debug(EdkLogger.DEBUG_9, "Generated [%s] files for module %s [%s]" %
                            (" ".join(AutoGenList), self.Name, self.Arch))
        elif AutoGenList == []:
            EdkLogger.debug(EdkLogger.DEBUG_9, "Skipped the generation of [%s] files for module %s [%s]" %
                            (" ".join(IgoredAutoGenList), self.Name, self.Arch))
        else:
            EdkLogger.debug(EdkLogger.DEBUG_9, "Generated [%s] (skipped %s) files for module %s [%s]" %
                            (" ".join(AutoGenList), " ".join(IgoredAutoGenList), self.Name, self.Arch))

        self.IsCodeFileCreated = True
        return AutoGenList

    ## Summarize the ModuleAutoGen objects of all libraries used by this module
    @cached_property
    def LibraryAutoGenList(self):
        RetVal = []
        for Library in self.DependentLibraryList:
            La = ModuleAutoGen(
                        self.Workspace,
                        Library.MetaFile,
                        self.BuildTarget,
                        self.ToolChain,
                        self.Arch,
                        self.PlatformInfo.MetaFile,
                        self.DataPipe
                        )
            La.IsLibrary = True
            if La not in RetVal:
                RetVal.append(La)
                for Lib in La.CodaTargetList:
                    self._ApplyBuildRule(Lib.Target, TAB_UNKNOWN_FILE)
        return RetVal

    def GenModuleHash(self):
        # Initialize a dictionary for each arch type
        if self.Arch not in GlobalData.gModuleHash:
            GlobalData.gModuleHash[self.Arch] = {}

        # Early exit if module or library has been hashed and is in memory
        if self.Name in GlobalData.gModuleHash[self.Arch]:
            return GlobalData.gModuleHash[self.Arch][self.Name].encode('utf-8')

        # Initialze hash object
        m = hashlib.md5()

        # Add Platform level hash
        m.update(GlobalData.gPlatformHash.encode('utf-8'))

        # Add Package level hash
        if self.DependentPackageList:
            for Pkg in sorted(self.DependentPackageList, key=lambda x: x.PackageName):
                if Pkg.PackageName in GlobalData.gPackageHash:
                    m.update(GlobalData.gPackageHash[Pkg.PackageName].encode('utf-8'))

        # Add Library hash
        if self.LibraryAutoGenList:
            for Lib in sorted(self.LibraryAutoGenList, key=lambda x: x.Name):
                if Lib.Name not in GlobalData.gModuleHash[self.Arch]:
                    Lib.GenModuleHash()
                m.update(GlobalData.gModuleHash[self.Arch][Lib.Name].encode('utf-8'))

        # Add Module self
        f = open(str(self.MetaFile), 'rb')
        Content = f.read()
        f.close()
        m.update(Content)

        # Add Module's source files
        if self.SourceFileList:
            for File in sorted(self.SourceFileList, key=lambda x: str(x)):
                f = open(str(File), 'rb')
                Content = f.read()
                f.close()
                m.update(Content)

        GlobalData.gModuleHash[self.Arch][self.Name] = m.hexdigest()

        return GlobalData.gModuleHash[self.Arch][self.Name].encode('utf-8')

    ## Decide whether we can skip the ModuleAutoGen process
    def CanSkipbyHash(self):
        # Hashing feature is off
        if not GlobalData.gUseHashCache:
            return False

        # Initialize a dictionary for each arch type
        if self.Arch not in GlobalData.gBuildHashSkipTracking:
            GlobalData.gBuildHashSkipTracking[self.Arch] = dict()

        # If library or Module is binary do not skip by hash
        if self.IsBinaryModule:
            return False

        # .inc is contains binary information so do not skip by hash as well
        for f_ext in self.SourceFileList:
            if '.inc' in str(f_ext):
                return False

        # Use Cache, if exists and if Module has a copy in cache
        if GlobalData.gBinCacheSource and self.AttemptModuleCacheCopy():
            return True

        # Early exit for libraries that haven't yet finished building
        HashFile = path.join(self.BuildDir, self.Name + ".hash")
        if self.IsLibrary and not os.path.exists(HashFile):
            return False

        # Return a Boolean based on if can skip by hash, either from memory or from IO.
        if self.Name not in GlobalData.gBuildHashSkipTracking[self.Arch]:
            # If hashes are the same, SaveFileOnChange() will return False.
            GlobalData.gBuildHashSkipTracking[self.Arch][self.Name] = not SaveFileOnChange(HashFile, self.GenModuleHash(), True)
            return GlobalData.gBuildHashSkipTracking[self.Arch][self.Name]
        else:
            return GlobalData.gBuildHashSkipTracking[self.Arch][self.Name]

    ## Decide whether we can skip the ModuleAutoGen process
    #  If any source file is newer than the module than we cannot skip
    #
    def CanSkip(self):
        if self.MakeFileDir in GlobalData.gSikpAutoGenCache:
            return True
        if not os.path.exists(self.TimeStampPath):
            return False
        #last creation time of the module
        DstTimeStamp = os.stat(self.TimeStampPath)[8]

        SrcTimeStamp = self.Workspace._SrcTimeStamp
        if SrcTimeStamp > DstTimeStamp:
            return False

        with open(self.TimeStampPath,'r') as f:
            for source in f:
                source = source.rstrip('\n')
                if not os.path.exists(source):
                    return False
                if source not in ModuleAutoGen.TimeDict :
                    ModuleAutoGen.TimeDict[source] = os.stat(source)[8]
                if ModuleAutoGen.TimeDict[source] > DstTimeStamp:
                    return False
        GlobalData.gSikpAutoGenCache.add(self.MakeFileDir)
        return True

    @cached_property
    def TimeStampPath(self):
        return os.path.join(self.MakeFileDir, 'AutoGenTimeStamp')
