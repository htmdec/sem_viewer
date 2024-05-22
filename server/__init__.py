#!/usr/bin/env python
# -*- coding: utf-8 -*-

import base64
import io
import json
import os
import re

import dateutil.parser
import magic
from bson import ObjectId
from girder import events, logger
from girder.api import access, rest
from girder.api.describe import autoDescribeRoute, Description
from girder.api.rest import boundHandler, setResponseHeader
from girder.constants import AccessType
from girder.exceptions import FilePathException, ValidationException
from girder.models.assetstore import Assetstore
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.item import Item
from girder.models.model_base import ModelImporter
from girder.utility import assetstore_utilities, toBool
from girder.utility.progress import ProgressContext
from PIL import Image, UnidentifiedImageError


@rest.boundHandler
def import_sem_data(self, event):
    params = event.info["params"]
    if params.get("dataType") not in ("sem", "pdv"):
        logger.warning("Importing using default importer")
        return

    data_type = params["dataType"]
    if data_type == "sem":
        import_cls = SEMHTMDECImporter
    elif data_type == "pdv":
        import_cls = PDVHTMDECImporter
    else:
        raise ValidationException(f"Unknown data type: {data_type}")
    logger.warning(f"Importing using {str(import_cls)} importer")

    if params["destinationType"] != "folder":
        raise ValidationException(
            f"{data_type} data can only be imported to girder folders"
        )

    importPath = params.get("importPath")
    if not os.path.exists(importPath):
        raise ValidationException("Not found: %s." % importPath)
    if not os.path.isdir(importPath):
        raise ValidationException("Not a directory: %s." % importPath)

    progress = toBool(params.get("progress", "false"))
    user = self.getCurrentUser()
    assetstore = Assetstore().load(event.info["id"])
    adapter = assetstore_utilities.getAssetstoreAdapter(assetstore)
    parent = self.model(params["destinationType"]).load(
        params["destinationId"], user=user, level=AccessType.ADMIN, exc=True
    )
    params["fileExcludeRegex"] = r"^_\..*"

    with ProgressContext(progress, user=user, title=f"{data_type} data import") as ctx:
        importer = import_cls(adapter, user, ctx, params=params)
        importer.import_data(parent, params["destinationType"], importPath)

    event.preventDefault().addResponse(None)


@access.public
@rest.boundHandler
def search_resources(self, event):
    params = event.info["params"]
    mode = params.get("mode", "text")
    if mode not in ("boundText", "jhuId"):
        return

    filters = {}
    for key, value in json.loads(params.get("filters", "{}")).items():
        if key.endswith("Id"):
            filters[key] = ObjectId(value)
        else:
            filters[key] = value

    user = self.getCurrentUser()
    limit, offset, sort = self.getPagingParameters(params, "name")
    types = json.loads(params.get("types", "[]"))
    level = params.get("level", AccessType.READ)
    level = AccessType.validate(level)

    if mode == "boundText":
        return boundText_search(event, user, filters, level, limit, offset, sort, types)
    elif mode == "jhuId":
        return jhuId_search(event, user, filters, level, limit, offset, sort, types)


def _get_model(modelName):
    if modelName not in ["item", "folder"]:
        return

    if "." in modelName:
        name, plugin = modelName.rsplit(".", 1)
        model = ModelImporter.model(name, plugin)
    else:
        model = ModelImporter.model(modelName)
    return model


def jhuId_search(event, user, filters, level, limit, offset, sort, types):
    params = event.info["params"]
    results = {}
    allowed = {
        "collection": ["_id", "name", "description"],
        "folder": ["_id", "name", "description", "meta", "parentId"],
        "item": ["_id", "name", "description", "meta", "folderId"],
        "user": ["_id", "firstName", "lastName", "login"],
    }

    for modelName in types:
        model = _get_model(modelName)
        if model is not None:
            query = {"meta.jhu_id": params.get("q")}
            query.update(filters)
            if hasattr(model, "filterResultsByPermission"):
                cursor = model.find(
                    query, fields=allowed[modelName] + ["public", "access"]
                )
                results[modelName] = list(
                    model.filterResultsByPermission(
                        cursor, user, level, limit=limit, offset=offset
                    )
                )
            else:
                results[modelName] = list(
                    model.find(
                        query,
                        fields=allowed[modelName],
                        limit=limit,
                        offset=offset,
                        sort=sort,
                    )
                )
    event.preventDefault().addResponse(results)


def boundText_search(event, user, filters, level, limit, offset, sort, types):
    params = event.info["params"]
    results = {}
    for modelName in types:
        model = _get_model(modelName)

        if model is not None:
            results[modelName] = [
                model.filter(d, user)
                for d in model.textSearch(
                    query=params.get("q"),
                    user=user,
                    limit=int(params.get("limit", "10")),
                    offset=int(params.get("offset", "0")),
                    level=level,
                    filters=filters,
                )
            ]

    event.preventDefault().addResponse(results)


class HTMDECImporter:
    def __init__(self, adapter, user, progress, params=None):
        self.adapter = adapter
        self.user = user
        self.progress = progress
        self.params = params or {}
        self.mime = magic.Magic(mime=True)

    def import_data(self, parent, parentType, importPath):
        for name in os.listdir(importPath):
            self.progress.update(message=name)
            path = os.path.join(importPath, name)
            if os.path.isdir(path):
                self.recurse_folder(parent, parentType, name, importPath)
            else:
                self.import_item(parent, parentType, name, importPath)

    def recurse_folder(self, parent, parentType, name, importPath):
        folder = Folder().createFolder(
            parent=parent,
            name=name,
            parentType=parentType,
            creator=self.user,
            reuseExisting=True,
        )
        nextPath = os.path.join(importPath, name)
        events.trigger(
            "filesystem_assetstore_imported",
            {"id": folder["_id"], "type": "folder", "importPath": nextPath},
        )
        self.import_data(folder, "folder", nextPath)


class PDVHTMDECImporter(HTMDECImporter):
    def import_item(self, parent, parentType, name, importPath):
        try:
            if date := re.search(r"\d{8}", name):
                date = dateutil.parser.parse(date.group())
                parent = Folder().createFolder(
                    parent=parent,
                    name=f"{date.year}",
                    parentType=parentType,
                    creator=self.user,
                    reuseExisting=True,
                )
                parent = Folder().createFolder(
                    parent=parent,
                    name=f"{date.year}{date.month:02d}{date.day:02d}",
                    parentType=parentType,
                    creator=self.user,
                    reuseExisting=True,
                )
        except dateutil.parser._parser.ParserError:
            pass

        item = Item().createItem(
            name=name, creator=self.user, folder=parent, reuseExisting=True
        )
        item = Item().setMetadata(item, {"pdv": True})

        fpath = os.path.join(importPath, name)
        events.trigger(
            "filesystem_assetstore_imported",
            {"id": item["_id"], "type": "item", "importPath": fpath},
        )
        if self.adapter.shouldImportFile(fpath, self.params):
            self.adapter.importFile(
                item, fpath, self.user, name=name, mimeType=self.mime.from_file(fpath)
            )


class SEMHTMDECImporter(HTMDECImporter):
    def import_item(self, parent, parentType, name, importPath):
        hdr_file = f"{name.replace('.tif', '-tif')}.hdr"
        if not os.path.isfile(os.path.join(importPath, hdr_file)):
            logger.warning(
                f"Importing {os.path.join(importPath, name)} failed because of missing header"
            )
            return
        item = Item().createItem(
            name=name, creator=self.user, folder=parent, reuseExisting=True
        )
        item = Item().setMetadata(item, {"sem": True})
        events.trigger(
            "filesystem_assetstore_imported",
            {
                "id": item["_id"],
                "type": "item",
                "importPath": os.path.join(importPath, name),
            },
        )
        for fname, mimeType in ((name, "image/tiff"), (hdr_file, "text/plain")):
            fpath = os.path.join(importPath, fname)
            if self.adapter.shouldImportFile(fpath, self.params):
                self.adapter.importFile(
                    item, fpath, self.user, name=fname, mimeType=mimeType
                )


def getTiffHeaderFromFile(path):
    try:
        with Image.open(path) as img:
            return next(
                (
                    _
                    for _ in img.tag_v2.values()
                    if isinstance(_, str) and "[User]" in _
                ),
                None,
            )
    except UnidentifiedImageError:
        pass


def getTiffHeaderFromItemMeta(item):
    fileId = item.get("meta", {}).get("headerId")
    if not fileId:
        return
    try:
        fobj = File().load(fileId, force=True, exc=True)
        with File().open(fobj) as fp:
            return fp.read().decode("utf-8")
    except Exception:
        pass


@access.user
@boundHandler
@autoDescribeRoute(
    Description("Get Tiff metadata from an item").modelParam(
        "id", model="item", level=AccessType.READ
    )
)
def get_tiff_metadata(self, item):
    try:
        child_file = list(Item().childFiles(item))[0]
    except IndexError:
        return
    try:
        path = File().getLocalFilePath(child_file)
    except FilePathException:
        path = None

    header = None
    if path:
        header = getTiffHeaderFromFile(path)

    if not header:
        header = getTiffHeaderFromItemMeta(item)

    if not header:
        header = "[MAIN]\r\nnoheader=1\r\n"

    setResponseHeader("Content-Type", "text/plain")
    return header


@access.user
@boundHandler
@autoDescribeRoute(
    Description("Get thumbnail for SEM data").modelParam(
        "id", model="item", level=AccessType.READ
    )
)
def get_sem_thumbnail(self, item):
    try:
        child_file = list(Item().childFiles(item))[0]
    except IndexError:
        return
    try:
        path = File().getLocalFilePath(child_file)
    except FilePathException:
        path = None

    if not path:
        return

    try:
        with Image.open(path, "r") as img:
            if img.mode != "RGB":
                img = img.convert(mode="I").point(lambda i: i * (1.0 / 256)).convert(mode="L")
            img.thumbnail((1000, 1000))
            fp = io.BytesIO()
            img.save(fp, format="PNG")
            return base64.b64encode(fp.getvalue()).decode()
    except UnidentifiedImageError:
        pass


@access.user
@boundHandler
@autoDescribeRoute(
    Description("Create folders recursively")
    .param("parentId", "The ID of the parent object", required=True)
    .param("parentType", "The type of the parent object", required=True)
    .param("path", "The path to create", required=True)
)
def create_folders(self, parentId, parentType, path):
    user = self.getCurrentUser()
    parent = ModelImporter.model(parentType).load(
        parentId, user=user, level=AccessType.WRITE, exc=True
    )
    for name in path.split("/"):
        parent = Folder().createFolder(
            parent=parent,
            name=name,
            parentType=parentType,
            creator=self.getCurrentUser(),
            reuseExisting=True,
        )
        parentType = "folder"
    return Folder().filter(parent, user)


def load(info):
    Item().exposeFields(level=AccessType.READ, fields="sem")
    File().ensureIndex(["sha512", {"sparse": False}])

    info["apiRoot"].item.route("GET", (":id", "tiff_metadata"), get_tiff_metadata)
    info["apiRoot"].item.route("GET", (":id", "tiff_thumbnail"), get_sem_thumbnail)
    info["apiRoot"].folder.route("POST", ("recursive",), create_folders)

    events.bind("rest.post.assetstore/:id/import.before", "sem_viewer", import_sem_data)
    events.bind("rest.get.resource/search.before", "sem_viewer", search_resources)
