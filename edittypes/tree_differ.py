import time

from anytree import PostOrderIter, NodeMixin
from anytree.util import leftsibling
import mwparserfromhell as mw

from .constants import *


# equivalent of main function
def get_diff(prev_wikitext, curr_wikitext, lang='en'):
    """Run through full process of getting tree diff between two wikitext revisions."""
    prev_tree = WikitextTree(wikitext=prev_wikitext, lang=lang)
    curr_tree = WikitextTree(wikitext=curr_wikitext, lang=lang)
    d = Differ(prev_tree, curr_tree)
    diff = d.get_corresponding_nodes()
    result = diff.post_process(prev_tree.secname_to_text, curr_tree.secname_to_text, lang=lang)
    return result


# helper functions for handling mwparserfromhell / wikitext
def simple_node_class(mwnode, lang='en'):
    """e.g., "<class 'mwparserfromhell.nodes.heading.Heading'>" -> "Heading"."""
    if type(mwnode) == str:
        return 'Text'
    else:
        nc = str(type(mwnode)).split('.')[-1].split("'")[0]
        if nc == 'Wikilink':
            n_prefix = mwnode.title.split(':', maxsplit=1)[0].lower()
            if n_prefix in [m.lower() for m in MEDIA_PREFIXES + MEDIA_ALIASES.get(lang, [])]:
                nc = 'Media'
            elif n_prefix in [c.lower() for c in CAT_PREFIXES + CAT_ALIASES.get(lang, [])]:
                nc = 'Category'
        return nc


def sec_to_name(mwsection, idx):
    """Converts a section to an interpretible and unique name."""
    return f'S#{idx}: {mwsection.nodes[0].title} (L{mwsection.nodes[0].level})'


def node_to_name(mwnode, lang='en'):
    """Converts a mwparserfromhell node to an interpretible name."""
    n_txt = mwnode.replace("\n", "\\n")
    if len(n_txt) > 13:
        return f'{simple_node_class(mwnode, lang)}: {n_txt[:10]}...'
    else:
        return f'{simple_node_class(mwnode, lang)}: {n_txt}'


def extract_text(mwnode, lang='en'):
    """Extract what text would be displayed from any node."""
    ntype = simple_node_class(mwnode, lang)
    if ntype == 'Text':
        return str(mwnode)
    elif ntype == 'HTMLEntity':
        return mwnode.normalize()
    elif ntype == 'Wikilink':
        if mwnode.text:
            return mwnode.text.strip_code()
        else:
            return mwnode.title.strip_code()
    elif ntype == 'ExternalLink' and mwnode.title:
        return mwnode.title.strip_code()
    # mwparserfromhell doesn't handle div/gallery well
    elif ntype == 'Tag' and mwnode.tag not in ('div', 'gallery'):
        return mwnode.contents.strip_code()
    else:  # Heading, Template, Comment, Argument, Category, Media, certain tags, URLs without display text
        return ''


def find_nested_media(wikitext, max_link_length=240):
    """Case-insensitive search for media files (lacking brackets) in wikitext -- i.e. in Templates and Galleries.

    For setting max_link_length: https://commons.wikimedia.org/wiki/Commons:File_naming#Length
    """
    lc_wt = wikitext.lower()
    media = []
    end = 0
    while True:
        m = EXTEN_PATTERN.search(lc_wt, pos=end)
        if m is None:
            break
        start, end = m.span()
        if end - start <= max_link_length:
            media.append(wikitext[start:end].strip())
    return media


class OrderedNode(NodeMixin):
    """
    Extension of anytree library node to support tree differ.
    """

    def __init__(self, name, ntype='Text', text_hash=None, idx=-1, text='', char_offset=-1, section=None,
                 parent=None, children=None):
        super(OrderedNode, self).__init__()
        self.name = name  # For debugging purposes
        self.ntype = ntype  # Different node types can be treated differently when computing equality
        self.text = str(text)  # Text that can then be passed to a diffing library
        # Used for quickly computing equality for most nodes.
        # Generally this just a simple hash of self.text (wikitext associated with a node) but
        # the text hash for sections and paragraphs is based on all the content within the section/paragraph
        # so it can be used for pruning while self.text is just the text that creates the section/paragraph
        # e.g., "==Section==\nThis is a section." would have as text "==Section==" but hash the full.
        # so the Differ doesn't identify a section/paragraph as changing when content within it is changed
        if text_hash is None:
            self.text_hash = hash(self.text)
        else:
            self.text_hash = hash(str(text_hash))
        self.idx = idx  # Used by Differ -- Post order on tree from 0...# nodes - 1
        self.char_offset = char_offset  # make it easy to find node in section text
        self.section = section  # section that the node is a part of -- useful for formatting final diff
        self.parent = parent
        if children:
            self.children = children

    def leftmost(self):
        return self.idx if self.is_leaf else self.children[0].leftmost()

    def unnest(self, lang='en'):
        """Build tree of document nodes by recursing within a single wikitext node.

        This approach starts with a single wikitext node -- e.g., a single Tag node with nested link nodes etc.:
        <ref>{{cite web|title=[[Gallery]]|url=http://digital.belvedere.at|publisher=Digitales Belvedere}}</ref>
        and splits it into its component pieces to then identify what has changed between revisions.

        Example above would take a Reference node as input and build the following tree (in-place):
        <--rest-of-tree-- Reference <--child-of-- Template (cite web) <--child-of-- WikiLink (Gallery)
                                                                ^--------child-of-- External Link (http://digital...)
        """
        wt = mw.parse(self.text)
        parent_node = self
        base_offset = self.char_offset
        parent_ranges = [(0, len(wt), self)]  # (start idx of node, end idx of node, node object)
        for idx, nn in enumerate(wt.ifilter(recursive=True)):
            if idx == 0:
                continue  # skip root node -- already set
            ntype = simple_node_class(nn, lang)
            if ntype == 'Text':
                # media w/o bracket will be IDed as text by mwparserfromhell
                # templates / galleries are where we find this nested media
                if self.ntype == 'Template' or (self.ntype == 'Tag' and self.text.startswith('<gallery')):
                    media = find_nested_media(str(nn))
                    for m in media:
                        nn_node = OrderedNode(f'Media: {m[:10]}...',
                                              ntype='Media',
                                              text=m,
                                              char_offset=base_offset + wt.find(str(m), parent_ranges[0][0]),
                                              section=self.section,
                                              parent=self)
            else:
                node_start = wt.find(str(nn), parent_ranges[0][0])  # start looking from the start of the latest node
                # identify direct parent of node
                for parent in parent_ranges:
                    if node_start < parent[1]:  # falls within parent range
                        parent_node = parent[2]
                        break
                nn_node = OrderedNode(node_to_name(nn, lang=lang), ntype=ntype, text=nn,
                                      char_offset=base_offset + node_start,
                                      section=self.section, parent=parent_node)
                parent_ranges.insert(0, (node_start, node_start + len(nn), nn_node))

    def dump(self):
        result = {'name': self.name,
                  'type': self.ntype,
                  'text': self.text,
                  'offset': self.char_offset,
                  'section': self.section}
        return result


class WikitextTree:
    """
    Tree structure for wikitext based on mwparserfromhell
    """

    def __init__(self, wikitext, lang="en"):
        self.lang = lang
        self.root = OrderedNode('root', ntype="Article")
        self.secname_to_text = {}
        self.wikitext_to_tree(wikitext)

    def wikitext_to_tree(self, wikitext):
        """Build tree of document nodes from Wikipedia article.

        This approach builds a tree with an artificial 'root' node on the 1st level,
        all of the article sections on the 2nd level (including an artificial Lede section),
        and all of the text, link, template, etc. nodes nested under their respective sections.
        """
        wt = mw.parse(wikitext)
        for sidx, s in enumerate(wt.get_sections(flat=True)):
            if s:
                sec_hash = sec_to_name(s, sidx)
                sec_text = ''.join([str(n) for n in s.nodes])
                self.secname_to_text[sec_hash] = sec_text
                s_node = OrderedNode(sec_hash, ntype="Heading", text=s.nodes[0], text_hash=sec_text, char_offset=0,
                                     section=sec_hash, parent=self.root)
                char_offset = len(s_node.text)
                for n in s.nodes[1:]:
                    n_node = OrderedNode(node_to_name(n, self.lang), ntype=simple_node_class(n, self.lang), text=n,
                                         char_offset=char_offset, section=s_node.name, parent=s_node)
                    char_offset += len(str(n))

    def expand_nested(self):
        """Expand nested nodes in tree -- e.g., Ref tags with templates/links contained in them."""
        for n in PostOrderIter(self.root):
            if n.ntype != 'Heading' and n.name != 'root' and n.ntype != 'Text':  # tag, link, etc.
                n.unnest(self.lang)


class Differ:
    """
    Find structural differences between two WikitextTrees
    """

    def __init__(self, t1, t2, timeout=2, expand_nodes=True):
        self.prune_trees(t1, t2, expand_nodes)
        self.t1 = []
        self.t2 = []
        for i, n in enumerate(PostOrderIter(t1.root)):
            n.idx = i
            self.t1.append(n)
        for i, n in enumerate(PostOrderIter(t2.root)):
            n.idx = i
            self.t2.append(n)
        self.timeout = time.time() + timeout
        self.ins_cost = 1
        self.rem_cost = 1
        self.chg_cost = 1
        self.nodetype_chg_cost = 10  # arbitrarily high to encourage remove+insert when node types change

        # Permanent store of transactions such that transactions[x][y] is the minimum
        # transactions to get from the sub-tree rooted at node x (in tree1) to the sub-tree
        # rooted at node y (in tree2).
        self.transactions = {None: {}}
        # Indices for each transaction, to avoid high performance cost of creating the
        # transactions multiple times
        self.transaction_to_idx = {None: {None: 0}}
        # All possible transactions
        self.idx_to_transaction = [(None, None)]

        idx_transaction = 1  # starts with nulls inserted

        transactions = {None: {None: []}}

        # Populate transaction stores
        for i in range(0, len(self.t1)):
            transactions[i] = {None: []}
            self.transaction_to_idx[i] = {None: idx_transaction}
            idx_transaction += 1
            self.idx_to_transaction.append((i, None))
            for j in range(0, len(self.t2)):
                transactions[None][j] = []
                transactions[i][j] = []
                self.transaction_to_idx[None][j] = idx_transaction
                idx_transaction += 1
                self.idx_to_transaction.append((None, j))
                self.transaction_to_idx[i][j] = idx_transaction
                idx_transaction += 1
                self.idx_to_transaction.append((i, j))
            self.transactions[i] = {}
        self.populate_transactions(transactions)

    def prune_trees(self, t1, t2, expand_nodes=False):
        """Quick heuristic preprocessing to reduce tree differ time by removing matching sections."""
        self.prune_sections(t1, t2)
        if expand_nodes:
            t1.expand_nested()
            t2.expand_nested()

    def prune_sections(self, t1, t2):
        """Prune nodes from any sections that align across revisions"""
        t1_sections = [n for n in PostOrderIter(t1.root) if n.ntype == "Heading"]
        t2_sections = [n for n in PostOrderIter(t2.root) if n.ntype == "Heading"]
        for secnode1 in t1_sections:
            for sn2_idx in range(len(t2_sections)):
                secnode2 = t2_sections[sn2_idx]
                if secnode1.text_hash == secnode2.text_hash:
                    # assumes sections aren't hierarchical in tree
                    # or if they are, the text_hash must also include nested sections
                    secnode1.children = []
                    secnode2.children = []
                    t2_sections.pop(sn2_idx)  # only match once
                    break

    def get_key_roots(self, tree):
        """Get keyroots (node has a left sibling or is the root) of a tree"""
        for on in tree:
            if on.is_root or leftsibling(on) is not None:
                yield on

    def populate_transactions(self, transactions):
        """Populate self.transactions with minimum transactions between all possible trees"""
        for kr1 in self.get_key_roots(self.t1):
            # Make transactions for tree -> null
            i_nulls = []
            for ii in range(kr1.leftmost(), kr1.idx + 1):
                i_nulls.append(self.transaction_to_idx[ii][None])
                transactions[ii][None] = i_nulls.copy()
            for kr2 in self.get_key_roots(self.t2):
                # Make transactions of null -> tree
                j_nulls = []
                for jj in range(kr2.leftmost(), kr2.idx + 1):
                    j_nulls.append(self.transaction_to_idx[None][jj])
                    transactions[None][jj] = j_nulls.copy()

                # get the diff
                self.find_minimum_transactions(kr1, kr2, transactions)
                if time.time() > self.timeout:
                    self.transactions = None
                    return

        for i in range(0, len(self.t1)):
            for j in range(0, len(self.t2)):
                if self.transactions.get(i, {}).get(j) and len(self.transactions[i][j]) > 0:
                    self.transactions[i][j] = tuple([self.idx_to_transaction[idx] for idx in self.transactions[i][j]])

    def get_node_distance(self, n1, n2):
        """
        Get the cost of:
        * removing a node from the first tree,
        * inserting a node into the second tree,
        * or relabelling a node from the first tree to a node from the second tree.
        """
        if n1 is None and n2 is None:
            return 0
        elif n1 is None:
            return self.ins_cost
        elif n2 is None:
            return self.rem_cost
        # Inserts/Removes are easy. Changes are more complicated and should only be within same node type.
        # Use arbitrarily high-value for nodetype changes to effectively ban.
        elif n1.ntype != n2.ntype:
            return self.nodetype_chg_cost
        # next two functions check if both nodes are the same (criteria varies by nodetype)
        elif n1.ntype == 'Heading':
            if n1.text == n2.text:
                return 0
            else:
                return self.chg_cost
        elif n1.text_hash == n2.text_hash:
            return 0
        # otherwise, same node types and not the same, then change cost
        else:
            return self.chg_cost

    def get_lowest_cost(self, rc, ic, cc):
        min_cost = rc
        index = 0
        if ic < min_cost:
            index = 1
            min_cost = ic
        if cc < min_cost:
            index = 2
        return index

    def find_minimum_transactions(self, kr1, kr2, transactions):
        """Find the minimum transactions to get from the first tree to the second tree."""
        for i in range(kr1.leftmost(), kr1.idx + 1):
            if i == kr1.leftmost():
                i_minus_1 = None
            else:
                i_minus_1 = i - 1
            n1 = self.t1[i]
            for j in range(kr2.leftmost(), kr2.idx + 1):
                if j == kr2.leftmost():
                    j_minus_1 = None
                else:
                    j_minus_1 = j - 1
                n2 = self.t2[j]

                if n1.leftmost() == kr1.leftmost() and n2.leftmost() == kr2.leftmost():
                    rem = transactions[i_minus_1][j]
                    ins = transactions[i][j_minus_1]
                    chg = transactions[i_minus_1][j_minus_1]
                    node_distance = self.get_node_distance(n1, n2)
                    # cost of each transaction
                    transaction = self.get_lowest_cost(len(rem) + self.rem_cost,
                                                       len(ins) + self.ins_cost,
                                                       len(chg) + node_distance)
                    if transaction == 0:
                        # record a remove
                        rc = rem.copy()
                        rc.append(self.transaction_to_idx[i][None])
                        transactions[i][j] = rc
                    elif transaction == 1:
                        # record an insert
                        ic = ins.copy()
                        ic.append(self.transaction_to_idx[None][j])
                        transactions[i][j] = ic
                    else:
                        # If nodes i and j are different, record a change, otherwise there is no transaction
                        transactions[i][j] = chg.copy()
                        if node_distance == 1:
                            transactions[i][j].append(self.transaction_to_idx[i][j])

                    self.transactions[i][j] = transactions[i][j].copy()
                else:
                    # Previous transactions, leading up to a remove, insert or change
                    rem = transactions[i_minus_1][j]
                    ins = transactions[i][j_minus_1]

                    if n1.leftmost() - 1 < kr1.leftmost():
                        k1 = None
                    else:
                        k1 = n1.leftmost() - 1
                    if n2.leftmost() - 1 < kr2.leftmost():
                        k2 = None
                    else:
                        k2 = n2.leftmost() - 1
                    chg = transactions[k1][k2]

                    transaction = self.get_lowest_cost(len(rem) + self.rem_cost,
                                                       len(ins) + self.ins_cost,
                                                       len(chg) + len(self.transactions[i][j]))
                    if transaction == 0:
                        # record a remove
                        rc = rem.copy()
                        rc.append(self.transaction_to_idx[i][None])
                        transactions[i][j] = rc
                    elif transaction == 1:
                        # record an insert
                        ic = ins.copy()
                        ic.append(self.transaction_to_idx[None][j])
                        transactions[i][j] = ic
                    else:
                        # record a change
                        cc = chg.copy()
                        cc.extend(self.transactions[i][j])
                        transactions[i][j] = cc

    def get_corresponding_nodes(self):
        """Explain transactions"""
        transactions = self.transactions[len(self.t1) - 1][len(self.t2) - 1]
        remove = []
        insert = []
        change = []
        for i in range(0, len(transactions)):
            if transactions[i][0] is None:
                ins_node = self.t2[transactions[i][1]]
                insert.append(ins_node)
            elif transactions[i][1] is None:
                rem_node = self.t1[transactions[i][0]]
                remove.append(rem_node)
            else:
                prev_node = self.t1[transactions[i][0]]
                curr_node = self.t2[transactions[i][1]]
                change.append((prev_node, curr_node))
        diff = {'remove': remove, 'insert': insert, 'change': change}
        self.detect_moves(diff)
        return Diff(nodes_removed=diff['remove'], nodes_inserted=diff['insert'],
                    nodes_changed=diff['change'], nodes_moved=diff['move'])

    def detect_moves(self, diff):
        """Detect when nodes were moved (as opposed to removed/inserted/changed) and update diff."""

        # build list of all prev and curr nodes to compare for matches
        prev_nodes = [('remove', i, pn) for i, pn in enumerate(diff['remove'])]
        curr_nodes = [('insert', j, cn) for j, cn in enumerate(diff['insert'])]
        for k in range(len(diff['change'])):
            prev_nodes.append(('change', k, diff['change'][k][0]))
            curr_nodes.append(('change', k, diff['change'][k][1]))

        # loop through prev/curr nodes and look for matches. constraints:
        # * nodes can only match with one other node
        # * if a node is part of a change, make sure it's corresponding node is moved to insert/remove accordingly
        prev_moved = []
        curr_moved = []
        curr_found = set()
        add_to_insert = {}
        add_to_remove = {}
        for pet, pidx, pn in prev_nodes:
            for cet, cidx, cn in curr_nodes:
                cid = f'{cet}-{cidx}'
                if pn.ntype == cn.ntype and pn.text_hash == cn.text_hash and cid not in curr_found:
                    prev_moved.append((pet, pidx))
                    curr_moved.append((cet, cidx))
                    curr_found.add(cid)
                    if pet == 'change':
                        corresponding_changed_node = diff['change'][pidx][1]
                        add_to_insert[cidx] = corresponding_changed_node
                    if cet == 'change':
                        corresponding_changed_node = diff['change'][cidx][0]
                        add_to_remove[pidx] = corresponding_changed_node
                    break

        # populate move list
        # if from a change, make sure it also isn't set to be moved to insert/remove
        diff['move'] = []
        if prev_moved:
            for i in range(len(prev_moved)):
                pet, pidx = prev_moved[i]
                cet, cidx = curr_moved[i]
                pn = diff[pet][pidx]
                if pet == 'change':  # pn is not the node but tuple of (prev_node, curr_node)
                    pn = pn[0]
                    if pidx in add_to_remove:  # don't add to remove -- was involved in its own move
                        add_to_remove.pop(pidx)
                cn = diff[cet][cidx]
                if cet == 'change':
                    cn = cn[1]
                    if cidx in add_to_insert:
                        add_to_insert.pop(cidx)
                diff['move'].append((pn, cn))
            prev_moved = sorted(prev_moved, reverse=True)
            for pet, pidx in prev_moved:
                diff[pet].pop(pidx)
            curr_moved = sorted(curr_moved, reverse=True)
            for cet, cidx in curr_moved:
                if (cet, cidx) not in prev_moved:  # was part of a change, already popped
                    diff[cet].pop(cidx)

            diff['insert'].extend(list(add_to_insert.values()))
            diff['remove'].extend(list(add_to_remove.values()))


class Diff:
    """
    Diff result with helper functions for post-processing / cleaning up the result
    """

    def __init__(self, nodes_removed, nodes_inserted, nodes_changed, nodes_moved):
        self.remove = [n.dump() for n in nodes_removed]
        self.insert = [n.dump() for n in nodes_inserted]
        self.change = [{'prev': pn.dump(), 'curr': cn.dump()} for pn, cn in nodes_changed]
        self.move = [{'prev': pn.dump(), 'curr': cn.dump()} for pn, cn in nodes_moved]

    def post_process(self, sections_prev, sections_curr, lang):
        self.merge_text_changes(sections_prev, sections_curr, lang)
        return self.dump(sections_prev, sections_curr)

    def dump(self, sections_prev, sections_curr):
        sp = {}
        sc = {}
        for n in self.remove:
            sec_name = n['section']
            sp[sec_name] = sections_prev[sec_name]
        for n in self.insert:
            sec_name = n['section']
            sc[sec_name] = sections_curr[sec_name]
        for n in self.change + self.move:
            pn = n['prev']
            cn = n['curr']
            psec_name = pn['section']
            sp[psec_name] = sections_prev[psec_name]
            csec_name = cn['section']
            sc[csec_name] = sections_curr[csec_name]

        return {'remove': self.remove, 'insert': self.insert, 'change': self.change, 'move': self.move,
                'sections-prev': sp, 'sections-curr': sc}

    def section_mapping(self, sections_prev, sections_curr):
        """Build mapping of sections between previous and current versions of article."""
        prev = list(sections_prev.keys())
        curr = list(sections_curr.keys())
        p_to_c = {}
        c_to_p = {}
        removed = []
        for n in self.remove:
            if n['type'] == 'Heading':
                for i, s in enumerate(prev):
                    if s == n['name']:
                        removed.append(i)
                        break
        for idx in sorted(removed, reverse=True):
            p_to_c[prev[idx]] = None
            prev.pop(idx)
        inserted = []
        for n in self.insert:
            if n['type'] == 'Heading':
                for i, s in enumerate(curr):
                    if s == n['name']:
                        inserted.append(i)
                        break
        for idx in sorted(inserted, reverse=True):
            c_to_p[curr[idx]] = None
            curr.pop(idx)

        # changes happen in place so don't effect structure of doc and can be ignored

        for c in self.move:
            pn = c['prev']
            cn = c['curr']
            if pn['type'] == 'Heading':
                prev_idx = None
                curr_idx = None
                for i, s in enumerate(prev):
                    if s == pn['name']:
                        prev_idx = i
                        break
                for i, s in enumerate(curr):
                    if s == cn['name']:
                        curr_idx = i
                        break
                if prev_idx is not None and curr_idx is not None:
                    s = curr.pop(curr_idx)
                    curr.insert(prev_idx, s)

        for i in range(len(prev)):
            p_to_c[prev[i]] = curr[i]
            c_to_p[curr[i]] = prev[i]

        return p_to_c, c_to_p

    def merge_text_changes(self, sections_prev, sections_curr, lang='en'):
        """Replace isolated text changes with section-level text changes."""
        p_to_c, c_to_p = self.section_mapping(sections_prev, sections_curr)
        changes = []
        prev_secs_checked = set()
        curr_secs_checked = set()
        for idx in range(len(self.remove) - 1, -1, -1):
            r = self.remove[idx]
            if r['type'] == 'Text':
                prev_sec = r['section']
                if prev_sec not in prev_secs_checked:
                    prev_secs_checked.add(prev_sec)
                    prev_text = ''.join([extract_text(n, lang) for n in mw.parse(sections_prev[prev_sec]).nodes])
                    curr_sec = p_to_c[prev_sec]
                    curr_secs_checked.add(curr_sec)
                    if curr_sec is None:
                        changes.append({'prev': {'name': node_to_name(prev_text), 'type': 'Text', 'text': prev_text,
                                                 'section': prev_sec, 'offset': 0}})
                    else:
                        curr_text = ''.join([extract_text(n, lang) for n in mw.parse(sections_curr[curr_sec]).nodes])
                        if prev_text != curr_text:
                            changes.append({'prev': {'name': node_to_name(prev_text), 'type': 'Text', 'text': prev_text,
                                                     'section': prev_sec, 'offset': 0},
                                            'curr': {'name': node_to_name(curr_text), 'type': 'Text', 'text': curr_text,
                                                     'section': curr_sec, 'offset': 0}})
                self.remove.pop(idx)
        for idx in range(len(self.insert) - 1, -1, -1):
            i = self.insert[idx]
            if i['type'] == 'Text':
                curr_sec = i['section']
                if curr_sec not in curr_secs_checked:
                    curr_secs_checked.add(curr_sec)
                    curr_text = ''.join([extract_text(n, lang) for n in mw.parse(sections_curr[curr_sec]).nodes])
                    prev_sec = c_to_p[curr_sec]
                    prev_secs_checked.add(prev_sec)
                    if prev_sec is None:
                        changes.append({'curr': {'name': node_to_name(curr_text), 'type': 'Text', 'text': curr_text,
                                                 'section': curr_sec, 'offset': 0}})
                    else:
                        prev_text = ''.join([extract_text(n, lang) for n in mw.parse(sections_prev[prev_sec]).nodes])
                        if prev_text != curr_text:
                            changes.append({'prev': {'name': node_to_name(prev_text), 'type': 'Text', 'text': prev_text,
                                                     'section': prev_sec, 'offset': 0},
                                            'curr': {'name': node_to_name(curr_text), 'type': 'Text', 'text': curr_text,
                                                     'section': curr_sec, 'offset': 0}})
                self.insert.pop(idx)
        for idx in range(len(self.change) - 1, -1, -1):
            pn = self.change[idx]['prev']
            cn = self.change[idx]['curr']
            if pn['type'] == 'Text':
                prev_sec = pn['section']
                if prev_sec not in prev_secs_checked:
                    prev_secs_checked.add(prev_sec)
                    prev_text = ''.join([extract_text(n, lang) for n in mw.parse(sections_prev[prev_sec]).nodes])
                    curr_sec = cn['section']
                    curr_text = ''.join([extract_text(n, lang) for n in mw.parse(sections_curr[curr_sec]).nodes])
                    if prev_text != curr_text:
                        changes.append({'prev': {'name': node_to_name(prev_text), 'type': 'Text', 'text': prev_text,
                                                 'section': prev_sec, 'offset': 0},
                                        'curr': {'name': node_to_name(curr_text), 'type': 'Text', 'text': curr_text,
                                                 'section': curr_sec, 'offset': 0}})
                self.change.pop(idx)

        for c in changes:
            if 'prev' in c and 'curr' in c:
                self.change.append(c)
            elif 'prev' in c:
                self.remove.append(c['prev'])
            elif 'curr' in c:
                self.insert.append(c['curr'])
