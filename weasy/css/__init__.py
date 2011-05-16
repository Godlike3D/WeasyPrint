# coding: utf8

#  WeasyPrint converts web documents (HTML, CSS, ...) to PDF.
#  Copyright (C) 2011  Simon Sapin
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as
#  published by the Free Software Foundation, either version 3 of the
#  License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
    weasy.css
    ---------
    
    This module takes care of steps 3 and 4 of “CSS 2.1 processing model”:
    Retrieve stylesheets associated with a document and annotate every element
    with a value for every CSS property.
    http://www.w3.org/TR/CSS21/intro.html#processing-model
    
    This module does this in more than two steps. The `annotate_document`
    function does everything, but there is also a function for each step:
    
     * ``find_stylesheets``: Find and parse all author stylesheets in a document
     * ``remove_ignored_declarations``: Remove illegal and unsupported
       declarations
     * ``expand_shorthand``: Replace shorthand properties
     * ``resolve_import_media``: Resolve @media and @import rules
     * ``apply_style_rule``: Apply a CSSStyleRule to a document
     * ``assign_properties``: Assign on computed value for each property to
       every DOM element.
"""

import os.path
try:
    from urlparse import urljoin
except ImportError:
    # Python 3
    from urllib.parse import urljoin

from cssutils import parseString, parseUrl, parseStyle, parseFile
from cssutils.css import CSSStyleDeclaration
from lxml import cssselect

from . import shorthands
from . import inheritance
from . import initial_values
from . import computed_values


HTML4_DEFAULT_STYLESHEET = parseFile(os.path.join(os.path.dirname(__file__),
    'html4_default.css'))


# Pseudo-classes and pseudo-elements are the same to lxml.cssselect.parse().
# List the identifiers for all CSS3 pseudo elements here to distinguish them.
PSEUDO_ELEMENTS = ('before', 'after', 'first-line', 'first-letter')


def find_stylesheets(html_document):
    """
    Yield stylesheets from a DOM document.
    """
    for element in html_document.iter():
        mimetype = element.get('type')
        # Only keep 'type/subtype' from 'type/subtype ; param1; param2'.
        if mimetype and mimetype.split(';', 1)[0].strip() != 'text/css':
            continue
        # cssutils translates '' to 'all'.
        media_attr = element.get('media', '').strip()
        if element.tag == 'style':
            # TODO: handle the `scoped` attribute
            # Content is text that is directly in the <style> element, not its
            # descendants
            content = [element.text]
            for child in element:
                content.append(child.tail)
            content = ''.join(content)
            # lxml should give us either unicode or ASCII-only bytestrings, so
            # we don't need `encoding` here.
            yield parseString(content, href=element.base_url,
                              media=media_attr, title=element.get('title'))
        elif element.tag == 'link' and element.get('href') \
                and ' stylesheet ' in ' %s ' % element.get('rel', ''):
            # URLs should NOT have been made absolute earlier
            # TODO: support the <base> HTML element, but do not use
            # lxml.html.HtmlElement.make_links_absolute() that changes the tree
            href = urljoin(element.base_url, element.get('href'))
            yield parseUrl(href, media=media_attr, title=element.get('title'))


def invalid_declaration_reason(prop):
    """
    Take a Property object and return a string describing the reason if it’s
    invalid, or None if it’s valid.
    """
    # TODO: validation


def find_rulesets(stylesheet):
    """
    Recursively walk a stylesheet and its @media and @import rules to yield
    all rulesets (CSSStyleRule or CSSPageRule objects).
    """
    for rule in stylesheet.cssRules:
        if rule.type in (rule.STYLE_RULE, rule.PAGE_RULE):
            yield rule
        elif rule.type == rule.IMPORT_RULE:
            for subrule in find_rulesets(rule.styleSheet):
                yield subrule
        elif rule.type == rule.MEDIA_RULE:
            # CSSMediaRule is kinda like a CSSStyleSheet: it has media and
            # cssRules attributes.
            for subrule in find_rulesets(rule):
                yield subrule
        # ignore everything else


def remove_ignored_declarations(stylesheet):
    """
    Changes IN-PLACE the given stylesheet and its imported stylesheets
    (recursively) to remove illegal or unsupported declarations.
    """
    # TODO: @font-face
    for rule in find_rulesets(stylesheet):
        new_style = CSSStyleDeclaration()
        for prop in rule.style:
            reason = invalid_declaration_reason(prop)
            if reason is None:
                new_style.setProperty(prop)
            # TODO: log ignored declarations, with reasons
        rule.style = new_style
    

def evaluate_media_query(query_list, medium):
    """
    Return the boolean evaluation of `query_list` for the given `medium`
    
    :attr query_list: a cssutilts.stlysheets.MediaList
    :attr medium: a media type string (for now)
    
    """
    # TODO: actual support for media queries, not just media types
    return query_list.mediaText == 'all' \
        or any(query.mediaText == medium for query in query_list)


def resolve_import_media(sheet, medium):
    """
    Resolves @import and @media rules in the given CSSStlyleSheet, and yields
    applicable rules for `medium`.
    """
    if not evaluate_media_query(sheet.media, medium):
        return
    for rule in sheet.cssRules:
        if rule.type in (rule.CHARSET_RULE, rule.COMMENT):
            continue # ignored
        elif rule.type == rule.IMPORT_RULE:
            subsheet = rule.styleSheet
        elif rule.type == rule.MEDIA_RULE:
            # CSSMediaRule is kinda like a CSSStyleSheet: it has media and
            # cssRules attributes.
            subsheet = rule
        else:
            # pass other rules through: "normal" rulesets, @font-face,
            # @namespace, @page, and @variables
            yield rule
            continue # no sub-stylesheet here.
        for subrule in resolve_import_media(subsheet, medium):
            yield subrule


def expand_shorthands(stylesheet):
    """
    Changes IN-PLACE the given stylesheet and its imported stylesheets
    (recursively) to expand shorthand properties.
    eg. margin becomes margin-top, margin-right, margin-bottom and margin-left.
    """
    for rule in find_rulesets(stylesheet):
        rule.style = shorthands.expand_shorthands_in_declaration(rule.style)
        # TODO: @font-face


def build_lxml_proxy_cache(document):
    """
    Build as needed a proxy cache for an lxml document.
    
    ``Element`` python objects in lxml are only proxies to C space memory
    (libxml2 data structures.) These objects may be created and destroyed at
    any time, so we can generally not keep state in them.

    The lxml documentation[1] only gives one guarantee: if a reference to a
    proxy object is kept, this same object will always be used and we can keep
    data there. A proxy cache is a list of these proxy objects for all elements,
    just to make sure that they are kept alive.

    [1] http://lxml.de/element_classes.html#element-initialization
    """
    if not hasattr(document, 'proxy_cache'):
        document.proxy_cache = list(document.iter())
    

def declaration_precedence(origin, priority):
    """
    Return the precedence for a rule. Precedence values have no meaning unless
    compared to each other.

    Acceptable values for `origin` are the strings 'author', 'user' and
    'user agent'.
    """
    # See http://www.w3.org/TR/CSS21/cascade.html#cascading-order
    if origin == 'user agent':
        return 1
    elif origin == 'user' and not priority:
        return 2
    elif origin == 'author' and not priority:
        return 3
    elif origin == 'author': # and priority
        return 4
    elif origin == 'user': # and priority
        return 5
    else:
        assert ValueError('Unkown origin: %r' % origin)


def apply_style_rule(rule, document, origin):
    """
    Apply a CSSStyleRule to a document according to its selectors, attaching
    Property objects with their precedence to DOM elements.
    
    Acceptable values for `origin` are the strings 'author', 'user' and
    'user agent'.
    """
    build_lxml_proxy_cache(document)
    selectors = []
    for selector in rule.selectorList:
        parsed_selector = cssselect.parse(selector.selectorText)
        # cssutils made sure that `selector` is not a "group of selectors"
        # in CSS3 terms (`rule.selectorList` is) so `parsed_selector` cannot be
        # of type `cssselect.Or`.
        # This leaves only three cases:
        #  * The selector ends with a pseudo-element. As `cssselect.parse()`
        #    parses left-to-right, `parsed_selector` is a `cssselect.Pseudo`
        #    instance that we can unwrap. This is the only place where CSS
        #    allows pseudo-element selectors.
        #  * The selector has a pseudo-element not at the end. This is invalid
        #    and the whole ruleset should be ignored.
        #  * The selector has no pseudo-element and is supported by
        #    `cssselect.CSSSelector`.
        if isinstance(parsed_selector, cssselect.Pseudo) \
                and parsed_selector.ident in PSEUDO_ELEMENTS:
            pseudo_type = parsed_selector.ident
            # Remove the pseudo-element from the selector
            parsed_selector = parsed_selector.element
        else:
            # No pseudo-element or invalid selector.
            pseudo_type = ''
        
        try:
            selector_callable = cssselect.CSSSelector(parsed_selector)
        except cssselect.ExpressionError:
            # Invalid selector, ignore the whole ruleset.
            # TODO: log this error.
            return
        selectors.append((selector_callable, pseudo_type, selector.specificity))
    
    # Only apply to elements after seeing all selectors, as we want to
    # ignore he whole ruleset if just one selector is invalid.
    # TODO: test that ignoring actually happens.
    for selector, pseudo_type, specificity in selectors:
        for element in selector(document):
            element = element.pseudo_elements[pseudo_type]
            for prop in rule.style:
                # TODO: ignore properties that do not apply to the current 
                # medium? http://www.w3.org/TR/CSS21/intro.html#processing-model
                precedence = (
                    declaration_precedence(origin, prop.priority),
                    specificity
                )
                element.applicable_properties.append((precedence, prop))


def handle_style_attribute(element):
    """
    Return the element’s ``applicable_properties`` list after adding properties
    from the `style` attribute.
    """
    style_attribute = element.get('style')
    if style_attribute:
        # TODO: no href for parseStyle. What about relative URLs?
        # CSS3 says we should resolve relative to the attribute:
        # http://www.w3.org/TR/css-style-attr/#interpret
        for prop in parseStyle(style_attribute):
            precedence = (
                declaration_precedence('author', prop.priority),
                # 1 for being a style attribute, 0 as there is no selector.
                (1, 0, 0, 0)
            )
            element.applicable_properties.append((precedence, prop))


class StyleDict(dict):
    """
    style.font_size == style['font-size'].value
    """
    def __getattr__(self, key):
        try:
            return self[key.replace('_', '-')].value
        except KeyError:
            raise AttributeError(key)

        
def assign_properties(document):
    """
    For every element of the document, take the properties left by
    ``apply_style_rule`` and assign computed values with respect to the cascade,
    declaration priority (ie. ``!important``) and selector specificity.
    """
    build_lxml_proxy_cache(document)
    for element in document.iter():
        handle_style_attribute(element)
        
        for element in element.pseudo_elements.itervalues():
            # If apply_style_rule() was called in appearance order, the 
            # stability of Python's sort fulfills rule 4 of the cascade.
            # This lambda has one parameter deconstructed as a tuple
            element.applicable_properties.sort(
                key=lambda (precedence, prop): precedence)
            element.style = style = StyleDict()
            for precedence, prop in element.applicable_properties:
                style[prop.name] = prop.propertyValue
            
            inheritance.handle_inheritance(element)    
            initial_values.handle_initial_values(element)
            computed_values.handle_computed_values(element)
        

class PseudoElement(object):
    """
    Objects that behaves somewhat like lxml Element objects and hold styles
    for pseudo-elements.
    """
    def __init__(self, parent, pseudo_type):
        self.parent = parent
        self.pseudo_element_type = pseudo_type
        self.applicable_properties = []
    
    def getparent(self):
        """Pseudo-elements inherit from the associated element."""
        return self.parent


class PseudoElementDict(dict):
    """
    Like a defaultdict, creates PseudoElement objects as needed.
    """
    def __init__(self, element):
        self[''] = element
    
    def __missing__(self, key):
        pseudo_element = PseudoElement(self[''], key)
        self[key] = pseudo_element
        return pseudo_element


def annotate_document(document, user_stylesheets=None,
                      ua_stylesheets=(HTML4_DEFAULT_STYLESHEET,),
                      medium='print'):
    """
    Do everything from finding author stylesheets in the given HTML document
    to parsing and applying them, to end up with a `style` attribute on
    every DOM element: a dictionary with values for all CSS 2.1 properties.
    
    Given stylesheets will be modified in place.
    """
    build_lxml_proxy_cache(document)
    # Do NOT use document.make_links_absolute() as it changes the tree,
    # and `content: attr(href)` should get the original attribute value.
    for element in document.iter():
        element.applicable_properties = []
        element.pseudo_elements = PseudoElementDict(element)
    author_stylesheets = find_stylesheets(document)
    for sheets, origin in ((author_stylesheets, 'author'),
                           (user_stylesheets or [], 'user'),
                           (ua_stylesheets or [], 'user agent')):
        for sheet in sheets:
            # TODO: UA and maybe user stylesheets might only need to be expanded
            # once, not for every document.
            remove_ignored_declarations(sheet)
            expand_shorthands(sheet)
            for rule in resolve_import_media(sheet, medium):
                if rule.type == rule.STYLE_RULE:
                    apply_style_rule(rule, document, origin)
                # TODO: handle @font-face, @namespace, @page, and @variables
    assign_properties(document)

