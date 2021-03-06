# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

import math

from cornice import Service
from pyramid.security import has_permission
from sqlalchemy.sql import or_

from bodhi import log
from bodhi.exceptions import BodhiException, LockedUpdateException
from bodhi.models import Update, Build, Bug, CVE, Package, UpdateRequest
import bodhi.schemas
import bodhi.security
from bodhi.validators import (
    validate_nvrs,
    validate_version,
    validate_uniqueness,
    validate_build_tags,
    validate_acls,
    validate_builds,
    validate_enums,
    validate_releases,
    validate_release,
    validate_username,
    validate_update_id,
    validate_requirements,
)


update = Service(name='update', path='/updates/{id}',
                 validators=(validate_update_id,),
                 description='Update submission service',
                 acl=bodhi.security.package_maintainers_only_acl,
                 cors_origins=bodhi.security.cors_origins_ro)

update_edit = Service(name='update_edit', path='/updates/{id}/edit',
                 validators=(validate_update_id,),
                 description='Update submission service',
                 acl=bodhi.security.package_maintainers_only_acl,
                 cors_origins=bodhi.security.cors_origins_rw)

updates = Service(name='updates', path='/updates/',
                  acl=bodhi.security.packagers_allowed_acl,
                  description='Update submission service',
                  cors_origins=bodhi.security.cors_origins_ro)

update_request = Service(name='update_request', path='/updates/{id}/request',
                         description='Update request service',
                         acl=bodhi.security.package_maintainers_only_acl,
                         cors_origins=bodhi.security.cors_origins_rw)


@update.get(accept=('application/json', 'text/json'), renderer='json')
@update.get(accept=('application/javascript'), renderer='jsonp')
@update.get(accept="text/html", renderer="update.html")
def get_update(request):
    """Return a single update from an id, title, or alias"""
    can_edit = has_permission('edit', request.context, request)
    return dict(update=request.validated['update'], can_edit=can_edit)


@update_edit.get(accept="text/html", renderer="new_update.html")
def get_update_for_editing(request):
    """Return a single update from an id, title, or alias for the edit form"""
    return dict(
        update=request.validated['update'],
        types=reversed(bodhi.models.UpdateType.values()),
        severities=reversed(bodhi.models.UpdateSeverity.values()),
        suggestions=reversed(bodhi.models.UpdateSuggestion.values()),
    )


@update_request.post(schema=bodhi.schemas.UpdateRequestSchema,
                     validators=(validate_enums, validate_update_id),
                     permission='edit', renderer='json')
def set_request(request):
    """Sets a specific :class:`bodhi.models.UpdateRequest` on a given update"""
    update = request.validated['update']
    action = request.validated['request']

    if update.locked:
        request.errors.add('body', 'request',
                           "Can't change request on a locked update")
        return

    if action is UpdateRequest.stable:
        settings = request.registry.settings
        result, reason = update.check_requirements(request.db, settings)
        if not result:
            request.errors.add('body', 'request',
                               'Requirement not met %s' % reason)
            return

    try:
        update.set_request(action, request.user.name)
    except BodhiException as e:
        request.errors.add('body', 'request', e.message)

    return dict(update=update)


@updates.get(schema=bodhi.schemas.ListUpdateSchema,
             accept=('application/json', 'text/json'), renderer='json',
             validators=(validate_release, validate_releases,
                         validate_enums, validate_username))
@updates.get(schema=bodhi.schemas.ListUpdateSchema,
             accept=('application/javascript'), renderer='jsonp',
             validators=(validate_release, validate_releases,
                         validate_enums, validate_username))
@updates.get(schema=bodhi.schemas.ListUpdateSchema,
             accept=('application/atom+xml'), renderer='rss',
             validators=(validate_release, validate_releases,
                         validate_enums, validate_username))
@updates.get(schema=bodhi.schemas.ListUpdateSchema,
             accept=('text/html'), renderer='updates.html',
             validators=(validate_release, validate_releases,
                         validate_enums, validate_username))
def query_updates(request):
    db = request.db
    data = request.validated
    query = db.query(Update)

    log.debug('query(%s)' % data)

    approved_since = data.get('approved_since')
    if approved_since is not None:
        query = query.filter(Update.date_approved >= approved_since)

    bugs = data.get('bugs')
    if bugs is not None:
        query = query.join(Update.bugs)
        query = query.filter(or_(*[Bug.bug_id==bug_id for bug_id in bugs]))

    critpath = data.get('critpath')
    if critpath is not None:
        query = query.filter(Update.critpath==critpath)

    cves = data.get('cves')
    if cves is not None:
        query = query.join(Update.cves)
        query = query.filter(or_(*[CVE.cve_id==cve_id for cve_id in cves]))

    like = data.get('like')
    if like is not None:
        query = query.filter(or_(*[
            Update.title.like('%%%s%%' % like)
        ]))

    locked = data.get('locked')
    if locked is not None:
        query = query.filter(Update.locked==locked)

    modified_since = data.get('modified_since')
    if modified_since is not None:
        query = query.filter(Update.date_modified >= modified_since)

    packages = data.get('packages')
    if packages is not None:
        query = query.join(Update.builds).join(Build.package)
        query = query.filter(or_(*[Package.name==pkg for pkg in packages]))

    builds = data.get('builds')
    if builds is not None:
        query = query.join(Update.builds)
        query = query.filter(or_(*[Build.nvr==build for build in builds]))

    pushed = data.get('pushed')
    if pushed is not None:
        query = query.filter(Update.pushed==pushed)

    pushed_since = data.get('pushed_since')
    if pushed_since is not None:
        query = query.filter(Update.date_pushed >= pushed_since)

    releases = data.get('releases')
    if releases is not None:
        query = query.filter(or_(*[Update.release==r for r in releases]))

    # This singular version of the plural "releases" is purely for bodhi1
    # backwards compat (mostly for RSS feeds) - threebean
    release = data.get('release')
    if release is not None:
        query = query.filter(Update.release==release)

    req = data.get('request')
    if req is not None:
        query = query.filter(Update.request==req)

    severity = data.get('severity')
    if severity is not None:
        query = query.filter(Update.severity==severity)

    status = data.get('status')
    if status is not None:
        query = query.filter(Update.status==status)

    submitted_since = data.get('submitted_since')
    if submitted_since is not None:
        query = query.filter(Update.date_submitted >= submitted_since)

    suggest = data.get('suggest')
    if suggest is not None:
        query = query.filter(Update.suggest==suggest)

    type = data.get('type')
    if type is not None:
        query = query.filter(Update.type==type)

    user = data.get('user')
    if user is not None:
        query = query.filter(Update.user==user)

    query = query.order_by(Update.date_submitted.desc())
    total = query.count()

    page = data.get('page')
    rows_per_page = data.get('rows_per_page')
    pages = int(math.ceil(total / float(rows_per_page)))
    query = query.offset(rows_per_page * (page - 1)).limit(rows_per_page)

    return dict(
        updates=query.all(),
        page=page,
        pages=pages,
        rows_per_page=rows_per_page,
        total=total,
        chrome=data.get('chrome'),
        display_user=data.get('display_user'),
    )


@updates.post(schema=bodhi.schemas.SaveUpdateSchema,
              permission='create', renderer='json',
              validators=(
                  validate_nvrs, validate_version, validate_builds,
                  validate_uniqueness, validate_build_tags, validate_acls,
                  validate_enums, validate_requirements))
def new_update(request):
    """ Save an update.

    This entails either creating a new update, or editing an existing one. To
    edit an existing update, the update's original title must be specified in
    the ``edited`` parameter.
    """
    data = request.validated
    log.debug('validated = %s' % data)

    # This has already been validated at this point, but we need to ditch
    # it since the models don't care about a csrf argument.
    data.pop('csrf_token')

    try:
        if data.get('edited'):
            log.info('Editing update: %s' % data['edited'])
            up = Update.edit(request, data)
        else:
            log.info('Creating new update: %s' % ' '.join(data['builds']))
            up = Update.new(request, data)
            log.debug('update = %r' % up)

    except LockedUpdateException as e:
        request.errors.add('body', 'builds', "%s" % e)
        return

    except Exception as e:
        log.exception(e)
        request.errors.add('body', 'builds', 'Unable to create update')
        return

    up.obsolete_older_updates(request)

    return up
