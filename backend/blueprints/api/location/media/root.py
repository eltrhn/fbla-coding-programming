import sanic
from sanic_jwt import decorators as jwtdec

from . import uid_get, rqst_get
from . import MediaType

root = sanic.Blueprint('location_media_api', url_prefix='')


@root.get('/')
@uid_get('location')
@rqst_get('cont')
@jwtdec.protected()
async def all_location_items(rqst, location, *, cont):
    """
    Serves all media items, in groups of 5 (paginated according to 'cont', i.e. what
    page to continue from).
    """
    return sanic.response.json({'items': await location.items(cont=int(cont))}, status=200)


@root.get('/search')
@rqst_get('title', 'genre', 'media_type', 'author', 'cont')
@uid_get('location')
@jwtdec.protected()
async def search_location_media(rqst, location, *, title, genre, media_type, author, cont):
    """
    Implements item search functionality, serving a list of items
    that match the given search query.
    Also in groups of 5.
    """
    return sanic.response.json(
      await location.search(
        title=None if title == 'null' else title,
        genre=None if genre == 'null' else genre,
        type_=None if media_type == 'null' else media_type,
        author=None if author == 'null' else author,
        cont=cont
        ),
      status=200)


@root.post('/add')
@rqst_get('user', 'title', 'author', 'published', 'media_type', 'genre', 'isbn', 'price', 'length')
@jwtdec.protected()
async def add_media_item_to_db(rqst, user, *, title, author, published, media_type: {'name': str, 'limits': list}, genre, isbn, price, length):
    """
    Adds a new media item, taking all of its attributes.
    """
    if not user.perms.can_manage_media:
        sanic.exceptions.abort(403, "You aren't allowed to add media.")
    try:
        type_ = await MediaType(media_type['name'], user.location, rqst.app)
    except ValueError:
        type_ = await user.location.add_media_type(**type_)
    item = await user.location.add_media(title, author, published, type_, genre, isbn, price, length)
    return sanic.response.json({'mid': item.mid, 'image': item.image}, status=200)


@root.post('/remove')
@rqst_get('item', 'user')
@jwtdec.protected()
async def remove_media_item_from_db(rqst, user, *, item):
    """
    Deletes a media item altogether, clearing its fines and everything.
    """
    if not user.perms.can_manage_media:
        sanic.exceptions.abort(403, "You aren't allowed to remove media.")
    await item.remove()
    return sanic.response.raw(b'', status=204)
