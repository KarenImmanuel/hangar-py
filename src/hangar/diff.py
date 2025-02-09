from itertools import starmap
from typing import Iterable
from typing import List
from typing import NamedTuple
from typing import Set
from typing import Tuple
from typing import Union

import lmdb

from .context import TxnRegister
from .records import commiting
from .records import heads
from .records.commiting import tmp_cmt_env
from .records.parsing import MetadataRecordKey
from .records.parsing import RawDataRecordKey
from .records.parsing import arrayset_record_schema_raw_key_from_db_key
from .records.parsing import arrayset_record_schema_raw_val_from_db_val
from .records.parsing import data_record_raw_key_from_db_key
from .records.parsing import data_record_raw_val_from_db_val
from .records.parsing import metadata_record_raw_key_from_db_key
from .records.parsing import metadata_record_raw_val_from_db_val
from .records.queries import RecordQuery

# ------------------------- Differ Types --------------------------------------


HistoryDiffStruct = NamedTuple('HistoryDiffStruct', [('masterHEAD', str),
                                                     ('devHEAD', str),
                                                     ('ancestorHEAD', str),
                                                     ('canFF', bool)])

Changes = NamedTuple('Changes', [
    ('schema', dict),
    ('samples', dict),
    ('metadata', dict),
])

DiffOutDB = NamedTuple('DiffOutDB', [
    ('added', Set[Tuple[bytes, bytes]]),
    ('deleted', Set[Tuple[bytes, bytes]]),
    ('mutated', Set[Tuple[bytes, bytes]]),
])

DiffOut = NamedTuple('DiffOut', [
    ('added', Changes),
    ('deleted', Changes),
    ('mutated', Changes),
])

ConflictKeys = Union[str, RawDataRecordKey, MetadataRecordKey]

Conflicts = NamedTuple('Conflicts', [
    ('t1', Iterable[ConflictKeys]),
    ('t21', Iterable[ConflictKeys]),
    ('t22', Iterable[ConflictKeys]),
    ('t3', Iterable[ConflictKeys]),
    ('conflict', bool),
])
Conflicts.__doc__ = 'Four types of conflicts are accessible through this object.'
Conflicts.t1.__doc__ = 'Addition of key in master AND dev with different values.'
Conflicts.t21.__doc__ = 'Removed key in master, mutated value in dev.'
Conflicts.t22.__doc__ = 'Removed key in dev, mutated value in master.'
Conflicts.t3.__doc__ = 'Mutated key in both master AND dev to different values.'
Conflicts.conflict.__doc__ = 'Bool indicating if any type of conflict is present.'

DiffAndConflictsDB = NamedTuple('DiffAndConflictsDB', [
    ('diff', DiffOutDB),
    ('conflict', Conflicts),
])

DiffAndConflicts = NamedTuple('DiffAndConflicts', [
    ('diff', DiffOut),
    ('conflict', Conflicts),
])

# ------------------------------- Differ Methods ------------------------------


def diff_envs(base_env: lmdb.Environment, head_env: lmdb.Environment) -> DiffOutDB:
    """Main diff algorithm to determine changes between unpacked lmdb environments.

    Parameters
    ----------
    base_env : lmdb.Environment
        starting point to calculate changes from
    head_env : lmdb.Environment
        some commit which should be compared to BASE

    Returns
    -------
    DiffOutDB
        iterable of db formatted key/value pairs for `added`, `deleted`,
        `mutated` fields
    """
    added, deleted, mutated = [], [], []

    try:
        baseTxn = TxnRegister().begin_reader_txn(base_env)
        headTxn = TxnRegister().begin_reader_txn(head_env)
        baseCur = baseTxn.cursor()
        headCur = headTxn.cursor()
        moreBase = baseCur.first()
        moreHead = headCur.first()

        while True:
            if moreBase and moreHead:
                bKey, bVal = baseCur.item()
                hKey, hVal = headCur.item()
            elif (not moreBase) and (not moreHead):
                break
            # necessary to avoid deadlock at last items
            elif not moreBase:
                bKey = b'x'
                bVal = b''
                hKey, hVal = headCur.item()
            else:  # (not moreHead)
                hKey = b'x'
                hVal = b''
                bKey, bVal = baseCur.item()

            # inserted
            if bKey > hKey:
                added.append((hKey, hVal))
                moreHead = headCur.next()
                continue
            # deleted
            elif bKey < hKey:
                deleted.append((bKey, bVal))
                moreBase = baseCur.next()
                continue
            # no change
            elif (bKey == hKey) and (bVal == hVal):
                moreBase = baseCur.next()
                moreHead = headCur.next()
                continue
            # mutated
            else:  # (bKey == hKey) and (bVal != hVal)
                mutated.append((hKey, hVal))
                moreBase = baseCur.next()
                moreHead = headCur.next()
                continue

    finally:
        baseCur.close()
        headCur.close()
        base_env = TxnRegister().abort_reader_txn(base_env)
        head_env = TxnRegister().abort_reader_txn(head_env)

    return DiffOutDB(set(added), set(deleted), set(mutated))


def _raw_from_db_change(changes: Set[Tuple[bytes, bytes]]) -> Changes:
    """Perform conversion for records from db -> raw

    Parameters
    ----------
    changes : Set[Tuple[bytes, bytes]]
        iterable of db formatted key/value pairs

    Returns
    -------
    Changes
        human readable formatted dict of key/value pairs.
    """
    arraysets, metadata, schema = {}, {}, {}
    for k, v in changes:
        if k.startswith(b'a:'):
            rk = data_record_raw_key_from_db_key(k)
            rv = data_record_raw_val_from_db_val(v)
            arraysets[rk] = rv
            continue
        elif k.startswith(b'l:'):
            rk = metadata_record_raw_key_from_db_key(k)
            rv = metadata_record_raw_val_from_db_val(v)
            metadata[rk] = rv
            continue
        elif k.startswith(b's:'):
            rk = arrayset_record_schema_raw_key_from_db_key(k)
            rv = arrayset_record_schema_raw_val_from_db_val(v)
            schema[rk] = rv
            continue
    return Changes(schema=schema, samples=arraysets, metadata=metadata)


def _all_raw_from_db_changes(outDb: DiffAndConflictsDB) -> DiffAndConflicts:
    """Convert db formatted db diff/conflict results to human readable

    Parameters
    ----------
    outDb : DiffAndConflictsDB
        raw formatted structure containg `diff` and `conflict` fields

    Returns
    -------
    DiffAndConflicts
        Human readable struct containing ``diff`` and ``conflict`` fields.
    """
    it = (outDb.diff.added, outDb.diff.deleted, outDb.diff.mutated)
    out = map(_raw_from_db_change, it)  # significant perf improvement for large commits
    outRawDiff = DiffOut(*out)

    t1 = _raw_from_db_change(outDb.conflict.t1)
    t21 = _raw_from_db_change(outDb.conflict.t21)
    t22 = _raw_from_db_change(outDb.conflict.t22)
    t3 = _raw_from_db_change(outDb.conflict.t3)
    outRawConf = Conflicts(t1=t1, t21=t21, t22=t22, t3=t3, conflict=outDb.conflict.conflict)
    res = DiffAndConflicts(diff=outRawDiff, conflict=outRawConf)
    return res

# ------------------------- Commit Differ -------------------------------------


def _symmetric_difference_keys(pair1: Set[Tuple[bytes, bytes]],
                               pair2: Set[Tuple[bytes, bytes]]
                               ) -> List[Tuple[bytes]]:
    """Find all keys common to both input pairs AND which have different values.

    Essentially a moddified `symmetric_difference` set operation, which keeps
    track of all seen items. Note: This ignores any `count` tracking values in
    the input tuples (ie. lmdb keys ending in ":")

    Parameters
    ----------
    pair1 : Set[Tuple[bytes, bytes]]
        key/value pairs making up the first set
    pair2 : Set[Tuple[bytes, bytes]]
        key/value pairs making up the second set

    Returns
    -------
    List[Tuple[bytes]]
        keys which appear in both input pair sets but which have different values.
    """
    seen = set()
    conflict = []
    for k, v in pair1.symmetric_difference(pair2):
        if k in seen:
            conflict.append((k, v))
        else:
            seen.add(k)
    return conflict


def find_conflicts(master_diff: DiffOutDB, dev_diff: DiffOutDB) -> Conflicts:
    """Determine if/which type of conflicting changes occur in diverged commits.

    This function expects the output of :func:`diff_envs` for two commits
    between a base commit.

    Parameters
    ----------
    master_diff : DiffOutDB
        changes (adds, dels, mutations) between base and master HEAD
    dev_diff : DiffOutDB
        changes (adds, dels, mutations) between base and dev HEAD

    Returns
    -------
    Conflicts
        Tuple containing fields for `t1`, `t21`, `t22`, `t3`, and (bool)
        `conflicts` recording output info for if and what type of conflict has
        occured
    """
    t1 = _symmetric_difference_keys(master_diff.added, dev_diff.added)
    t21 = _symmetric_difference_keys(master_diff.deleted, dev_diff.mutated)
    t22 = _symmetric_difference_keys(master_diff.mutated, dev_diff.deleted)
    t3 = _symmetric_difference_keys(master_diff.mutated, dev_diff.mutated)
    isConflict = bool(any([t1, t21, t22, t3]))

    res = Conflicts(t1=t1, t21=t21, t22=t22, t3=t3, conflict=isConflict)
    return res


# ---------------------------- Differ Base  -----------------------------------


class BaseUserDiff(object):

    def __init__(self, branchenv: lmdb.Environment, refenv: lmdb.Environment, *args, **kwargs):

        self._branchenv: lmdb.Environment = branchenv
        self._refenv: lmdb.Environment = refenv

    def _determine_ancestors(self, mHEAD: str, dHEAD: str) -> HistoryDiffStruct:
        """Search the commit history to determine the closest common ancestor.

        The closest common ancestor is important because it serves as the "merge
        base" in a 3-way merge strategy. This is a very naive implementation, but it
        works well enough right now for simple branch histories.

        Parameters
        ----------
        mHEAD : str
            full commit hash to use as the `master` branch head commit
        dHEAD : str
            full commit hash to use as the `dev` branch head commit

        Returns
        -------
        HistoryDiffStruct
            indicating the masterHEAD, devHEAD, ancestorHEAD, and canFF which
            tells if this is a fast-forward-able commit.
        """

        mAncestors = commiting.get_commit_ancestors_graph(self._refenv, mHEAD)
        dAncestors = commiting.get_commit_ancestors_graph(self._refenv, dHEAD)
        cAncestors = set(mAncestors.keys()).intersection(set(dAncestors.keys()))
        canFF = True if mHEAD in cAncestors else False

        ancestorOrder = []
        for ancestor in cAncestors:
            timeOfCommit = commiting.get_commit_spec(self._refenv, ancestor).commit_time
            ancestorOrder.append((ancestor, timeOfCommit))

        ancestorOrder.sort(key=lambda t: t[1], reverse=True)
        commonAncestor = ancestorOrder[0][0]
        res = HistoryDiffStruct(
            masterHEAD=mHEAD, devHEAD=dHEAD, ancestorHEAD=commonAncestor, canFF=canFF)
        return res

    def _diff3(self,
               a_env: lmdb.Environment,
               m_env: lmdb.Environment,
               d_env: lmdb.Environment) -> DiffAndConflictsDB:
        """Three way diff and conflict finder from ancestor, master, and dev commits.

        Parameters
        ----------
        a_env : lmdb.Environment
            unpacked lmdb environment for the ancestor commit
        m_env : lmdb.Environment
            unpacked lmdb environment for the master commit, current HEAD
        d_env : lmdb.Environment
            unpacked lmdb environment for the dev commit, compare to HEAD

        Returns
        -------
        DiffAndConflictsDB
            structure containing (`additions`, `deletions`, `mutations`) for
            diff, as well as the ConflictRecord struct.
        """
        # it = (m_diff, d_diff, md_diff)
        it = ((a_env, m_env), (a_env, d_env), (d_env, m_env))
        diffs = list(starmap(diff_envs, it))  # significant perf improvement by map.
        conflict = find_conflicts(diffs[0], diffs[1])
        return DiffAndConflictsDB(diff=diffs[2], conflict=conflict)

    def _diff(self,
              a_env: lmdb.Environment,
              m_env: lmdb.Environment) -> DiffAndConflictsDB:
        """Fast Forward differ from ancestor to master commit.

        Note: this method returns the same MasterDevDiff struct as the three
        way commit diff method, but the `dev` and `conflicts` fields will be
        empty

        Parameters
        ----------
        a_env : lmdb.Environment
            unpacked lmdb environment for the ancestor commit
        m_env : lmdb.Environment
            unpacked lmdb environment for the master commit

        Returns
        -------
        DiffAndConflictsDB
            structure containing (`additions`, `deletions`, `mutations`) for
            the ancestor -> master (head) env diff
        """
        m_diff = diff_envs(a_env, m_env)
        conflict = Conflicts(t1=[], t21=[], t22=[], t3=[], conflict=False)
        return DiffAndConflictsDB(diff=m_diff, conflict=conflict)


# ------------------------ Read-Only Checkouts Only ---------------------------


class ReaderUserDiff(BaseUserDiff):
    """Methods diffing contents of a :class:`~hangar.checkout.ReaderCheckout` instance.

    These provide diffing implementations to compare the current checkout
    ``HEAD`` of a to a branch or commit. The results are generally returned as
    a nested set of named tuples.

    When diffing of commits or branches is performed, if there is not a linear
    history of commits between current ``HEAD`` and the diff commit (ie. a
    history which would permit a ``"fast-forward" merge``), the result field
    named ``conflict`` will contain information on any merge conflicts that
    would exist if staging area ``HEAD`` and the (compared) ``"dev" HEAD`` were
    merged "right now". Though this field is present for all diff comparisons,
    it can only contain non-empty values in the cases where a three way merge
    would need to be performed.

    ::

       Fast Forward is Possible
       ========================

           (master)          (foo)
       a ----- b ----- c ----- d


       3-Way Merge Required
       ====================

                            (master)
       a ----- b ----- c ----- d
               \\
                \\               (foo)
                 \\----- ee ----- ff
    """

    def __init__(self, commit_hash, *args, **kwargs):

        super().__init__(*args, **kwargs)
        self._commit_hash = commit_hash

    def _commit(self, dev_commit_hash: str) -> DiffAndConflictsDB:
        """Compute diff between head and commit hash, returning DB formatted results

        Parameters
        ----------
        dev_commit_hash : str
            hash of the commit to be used as the comparison.

        Returns
        -------
        DiffAndConflictsDB
            two-tuple of `diff`, `conflict` (if any) calculated in the diff
            algorithm.

        Raises
        ------
        ValueError
            if the specified `dev_commit_hash` is not a valid commit reference.
        """
        if not commiting.check_commit_hash_in_history(self._refenv, dev_commit_hash):
            msg = f'HANGAR VALUE ERROR: dev_commit_hash: {dev_commit_hash} does not exist'
            raise ValueError(msg)

        hist = self._determine_ancestors(self._commit_hash, dev_commit_hash)
        mH, dH, aH = hist.masterHEAD, hist.devHEAD, hist.ancestorHEAD
        with tmp_cmt_env(self._refenv, mH) as m_env, tmp_cmt_env(self._refenv, dH) as d_env:
            if hist.canFF is True:
                outDb = self._diff(m_env, d_env)
            else:
                with tmp_cmt_env(self._refenv, aH) as a_env:
                    outDb = self._diff3(a_env, m_env, d_env)
        return outDb

    def _branch(self, dev_branch: str) -> DiffAndConflictsDB:
        """Compute diff between ``HEAD`` and branch name, returning DB formatted results.

        Parameters
        ----------
        dev_branch : str
            name of the branch whose HEAD will be used to calculate the diff of.

        Returns
        -------
        DiffAndConflictsDB
            two-tuple of `diff`, `conflict` (if any) calculated in the diff
            algorithm.

        Raises
        ------
        ValueError
            If the specified `dev_branch` does not exist.
        """
        branchNames = heads.get_branch_names(self._branchenv)
        if dev_branch in branchNames:
            dHEAD = heads.get_branch_head_commit(self._branchenv, dev_branch)
        else:
            msg = f'HANGAR VALUE ERROR: dev_branch: {dev_branch} invalid branch name'
            raise ValueError(msg)

        hist = self._determine_ancestors(self._commit_hash, dHEAD)
        mH, dH, aH = hist.masterHEAD, hist.devHEAD, hist.ancestorHEAD
        with tmp_cmt_env(self._refenv, mH) as m_env, tmp_cmt_env(self._refenv, dH) as d_env:
            if hist.canFF is True:
                outDb = self._diff(m_env, d_env)
            else:
                with tmp_cmt_env(self._refenv, aH) as a_env:
                    outDb = self._diff3(a_env, m_env, d_env)

        return outDb

    def commit(self, dev_commit_hash: str) -> DiffAndConflicts:
        """Compute diff between HEAD and commit hash, returning user-facing results.

        Parameters
        ----------
        dev_commit_hash : str
            hash of the commit to be used as the comparison.

        Returns
        -------
        DiffAndConflicts
            two-tuple of ``diff``, ``conflict`` (if any) calculated in the diff
            algorithm.

        Raises
        ------
        ValueError
            if the specified ``dev_commit_hash`` is not a valid commit reference.
        """
        outDb = self._commit(dev_commit_hash=dev_commit_hash)
        outRaw = _all_raw_from_db_changes(outDb)
        return outRaw

    def branch(self, dev_branch: str) -> DiffAndConflicts:
        """Compute diff between HEAD and branch name, returning user-facing results.

        Parameters
        ----------
        dev_branch : str
            name of the branch whose HEAD will be used to calculate the diff of.

        Returns
        -------
        DiffAndConflicts
            two-tuple of ``diff``, ``conflict`` (if any) calculated in the diff
            algorithm.

        Raises
        ------
        ValueError
            If the specified `dev_branch` does not exist.
        """
        outDb = self._branch(dev_branch=dev_branch)
        outRaw = _all_raw_from_db_changes(outDb)
        return outRaw


# ---------------------- Write Enabled Checkouts Only -------------------------


class WriterUserDiff(BaseUserDiff):
    """Methods diffing contents of a :class:`~hangar.checkout.WriterCheckout` instance.

    These provide diffing implementations to compare the current ``HEAD`` of a
    checkout to a branch, commit, or the staging area ``"base"`` contents. The
    results are generally returned as a nested set of named tuples. In
    addition, the :meth:`status` method is implemented which can be used to
    quickly determine if there are any uncommitted changes written in the
    checkout.

    When diffing of commits or branches is performed, if there is not a linear
    history of commits between current ``HEAD`` and the diff commit (ie. a
    history which would permit a ``"fast-forward" merge``), the result field
    named ``conflict`` will contain information on any merge conflicts that
    would exist if staging area ``HEAD`` and the (compared) ``"dev" HEAD`` were
    merged "right now". Though this field is present for all diff comparisons,
    it can only contain non-empty values in the cases where a three way merge
    would need to be performed.

    ::

       Fast Forward is Possible
       ========================

           (master)          (foo)
       a ----- b ----- c ----- d


       3-Way Merge Required
       ====================

                            (master)
       a ----- b ----- c ----- d
               \\
                \\               (foo)
                 \\----- ee ----- ff
    """

    def __init__(self, stageenv: lmdb.Environment, branch_name: str, *args, **kwargs):

        super().__init__(*args, **kwargs)
        self._stageenv: lmdb.Environment = stageenv
        self._branch_name: str = branch_name

    def _commit(self, dev_commit_hash: str) -> DiffAndConflictsDB:
        """Compute diff between head and commit, returning DB formatted results.

        Parameters
        ----------
        dev_commit_hash : str
            hash of the commit to be used as the comparison.

        Returns
        -------
        DiffAndConflictsDB
            two-tuple of `diff`, `conflict` (if any) calculated in the diff
            algorithm.

        Raises
        ------
        ValueError
            if the specified `dev_commit_hash` is not a valid commit reference.
        """
        if not commiting.check_commit_hash_in_history(self._refenv, dev_commit_hash):
            msg = f'HANGAR VALUE ERROR: dev_commit_hash: {dev_commit_hash} does not exist'
            raise ValueError(msg)

        commit_hash = heads.get_branch_head_commit(self._branchenv, self._branch_name)
        hist = self._determine_ancestors(commit_hash, dev_commit_hash)
        with tmp_cmt_env(self._refenv, hist.devHEAD) as d_env:
            if hist.canFF is True:
                res = self._diff(self._stageenv, d_env)
            else:
                with tmp_cmt_env(self._refenv, hist.ancestorHEAD) as a_env:
                    res = self._diff3(a_env, self._stageenv, d_env)
        return res

    def _branch(self, dev_branch: str) -> DiffAndConflictsDB:
        """Compute diff between head and branch name, returning DB formatted results.

        Parameters
        ----------
        dev_branch : str
            name of the branch whose HEAD will be used to calculate the diff of.

        Returns
        -------
        DiffAndConflictsDB
            two-tuple of `diff`, `conflict` (if any) calculated in the diff
            algorithm.

        Raises
        ------
        ValueError
            If the specified `dev_branch` does not exist.
        """
        branchNames = heads.get_branch_names(self._branchenv)
        if dev_branch in branchNames:
            dHEAD = heads.get_branch_head_commit(self._branchenv, dev_branch)
        else:
            msg = f'HANGAR VALUE ERROR: dev_branch: {dev_branch} invalid branch name'
            raise ValueError(msg)

        commit_hash = heads.get_branch_head_commit(self._branchenv, self._branch_name)
        hist = self._determine_ancestors(commit_hash, dHEAD)
        with tmp_cmt_env(self._refenv, hist.devHEAD) as d_env:
            if hist.canFF is True:
                res = self._diff(self._stageenv, d_env)
            else:
                with tmp_cmt_env(self._refenv, hist.ancestorHEAD) as a_env:
                    res = self._diff3(a_env, self._stageenv, d_env)
        return res

    def _staged(self) -> DiffAndConflictsDB:
        """Return diff of the staging area to base, returning db formatted results.

        Returns
        -------
        DiffAndConflictsDB
            two-tuple of `diff`, `conflict` (if any) calculated in the diff
            algorithm.
        """
        commit_hash = heads.get_branch_head_commit(self._branchenv, self._branch_name)
        with tmp_cmt_env(self._refenv, commit_hash) as base_env:
            res = self._diff(base_env, self._stageenv)
        return res

    def commit(self, dev_commit_hash: str) -> DiffAndConflicts:
        """Compute diff between HEAD and commit, returning user-facing results.

        Parameters
        ----------
        dev_commit_hash : str
            hash of the commit to be used as the comparison.

        Returns
        -------
        DiffAndConflicts
            two-tuple of ``diff``, ``conflict`` (if any) calculated in the diff
            algorithm.

        Raises
        ------
        ValueError
            if the specified ``dev_commit_hash`` is not a valid commit reference.
        """
        outDb = self._commit(dev_commit_hash=dev_commit_hash)
        outRaw = _all_raw_from_db_changes(outDb)
        return outRaw

    def branch(self, dev_branch: str) -> DiffAndConflicts:
        """Compute diff between HEAD and branch, returning user-facing results.

        Parameters
        ----------
        dev_branch : str
            name of the branch whose HEAD will be used to calculate the diff of.

        Returns
        -------
        DiffAndConflicts
            two-tuple of ``diff``, ``conflict`` (if any) calculated in the diff
            algorithm.

        Raises
        ------
        ValueError
            If the specified ``dev_branch`` does not exist.
        """
        outDb = self._branch(dev_branch=dev_branch)
        outRaw = _all_raw_from_db_changes(outDb)
        return outRaw

    def staged(self) -> DiffAndConflicts:
        """Return diff of staging area to base, returning user-facing results.

        Returns
        -------
        DiffAndConflicts
            two-tuple of ``diff``, ``conflict`` (if any) calculated in the diff
            algorithm.
        """
        outDb = self._staged()
        outRaw = _all_raw_from_db_changes(outDb)
        return outRaw

    def status(self) -> str:
        """Determine if changes have been made in the staging area

        If the contents of the staging area and it's parent commit are the
        same, the status is said to be "CLEAN". If even one arrayset or
        metadata record has changed however, the status is "DIRTY".

        Returns
        -------
        str
            "CLEAN" if no changes have been made, otherwise "DIRTY"
        """
        head_commit = heads.get_branch_head_commit(self._branchenv, self._branch_name)
        if head_commit == '':
            base_refs = ()
        else:
            base_refs = commiting.get_commit_ref(self._refenv, head_commit)

        stage_refs = tuple(RecordQuery(self._stageenv)._traverse_all_records())
        status = 'DIRTY' if (base_refs != stage_refs) else 'CLEAN'
        return status
