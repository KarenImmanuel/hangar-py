import os
import pytest
from hangar import Repository


def test_imports():
    import hangar
    from hangar import Repository


def test_starting_up_repo_warns_should_exist_no_args(managed_tmpdir):
    with pytest.warns(UserWarning):
        repo = Repository(path=managed_tmpdir)
    repo.init(user_name='tester', user_email='foo@test.bar', remove_old=True)
    assert repo.list_branches() == ['master']
    assert os.path.isdir(repo._repo_path)
    assert repo._repo_path == os.path.join(managed_tmpdir, '.hangar')
    co = repo.checkout(write=True)
    assert co.diff.status() == 'CLEAN'
    co.close()
    repo._env._close_environments()


def test_starting_up_repo_warns_should_exist_manual_args(managed_tmpdir):
    with pytest.warns(UserWarning):
        repo = Repository(path=managed_tmpdir, exists=True)
    repo.init(user_name='tester', user_email='foo@test.bar', remove_old=True)
    assert repo.list_branches() == ['master']
    assert os.path.isdir(repo._repo_path)
    assert repo._repo_path == os.path.join(managed_tmpdir, '.hangar')
    co = repo.checkout(write=True)
    assert co.diff.status() == 'CLEAN'
    co.close()
    repo._env._close_environments()


def test_starting_up_repo_does_not_warn_not_exist_manual_args(managed_tmpdir):
    with pytest.warns(None) as warn_recs:
        repo = Repository(path=managed_tmpdir, exists=False)
    assert len(warn_recs) == 0

    repo.init(user_name='tester', user_email='foo@test.bar', remove_old=True)
    assert repo.list_branches() == ['master']
    assert os.path.isdir(repo._repo_path)
    assert repo._repo_path == os.path.join(managed_tmpdir, '.hangar')
    co = repo.checkout(write=True)
    assert co.diff.status() == 'CLEAN'
    co.close()
    repo._env._close_environments()


def test_initial_read_checkout(managed_tmpdir):
    repo = Repository(path=managed_tmpdir, exists=False)
    repo.init(user_name='tester', user_email='foo@test.bar', remove_old=True)
    with pytest.raises(ValueError):
        repo.checkout()
    repo._env._close_environments()


def test_initial_arrayset(managed_tmpdir, randomsizedarray):
    repo = Repository(path=managed_tmpdir, exists=False)
    repo.init(user_name='tester', user_email='foo@test.bar', remove_old=True)

    w_checkout = repo.checkout(write=True)
    assert len(w_checkout.arraysets) == 0
    with pytest.raises(KeyError):
        w_checkout.arraysets['aset']
    aset = w_checkout.arraysets.init_arrayset('aset', prototype=randomsizedarray)
    assert aset._asetn == 'aset'
    w_checkout.close()
    repo._env._close_environments()


def test_empty_commit(managed_tmpdir, caplog):
    repo = Repository(path=managed_tmpdir, exists=False)
    repo.init(user_name='tester', user_email='foo@test.bar', remove_old=True)
    w_checkout = repo.checkout(write=True)
    with pytest.raises(RuntimeError):
        w_checkout.commit('this is a merge message')
    w_checkout.close()
    repo._env._close_environments()


def test_cannot_operate_without_repo_init(managed_tmpdir):
    repo = Repository(path=managed_tmpdir, exists=False)

    with pytest.raises(RuntimeError):
        repo.writer_lock_held()
    with pytest.raises(RuntimeError):
        repo.checkout()
    with pytest.raises(RuntimeError):
        repo.writer_lock_held()
    with pytest.raises(RuntimeError):
        repo.log()
    with pytest.raises(RuntimeError):
        repo.summary()
    with pytest.raises(RuntimeError):
        repo.merge('fail', 'master', 'nonexistant')
    with pytest.raises(RuntimeError):
        repo.create_branch('test')
    with pytest.raises(RuntimeError):
        repo.list_branches()
    with pytest.raises(RuntimeError):
        repo.force_release_writer_lock()

    with pytest.raises(RuntimeError):
        repo.remote.add('origin', 'foo')
    with pytest.raises(RuntimeError):
        repo.remote.remove('origin')
    with pytest.raises(RuntimeError):
        repo.remote.fetch('origin', 'master')
    with pytest.raises(RuntimeError):
        repo.remote.fetch_data('origin', branch='master')
    with pytest.raises(RuntimeError):
        repo.remote.list_all()
    with pytest.raises(RuntimeError):
        repo.remote.ping('origin')
    with pytest.raises(RuntimeError):
        repo.remote.push('origin', 'master')
    with pytest.raises(RuntimeError):
        repo.remove_branch('master')

    with pytest.raises(RuntimeError):
        repo.path
    with pytest.raises(RuntimeError):
        repo.version
    with pytest.raises(RuntimeError):
        repo.writer_lock_held

    assert repo._env.repo_is_initialized is False


def test_force_release_writer_lock(managed_tmpdir, monkeypatch):
    from hangar.records import heads

    repo = Repository(path=managed_tmpdir, exists=False)
    repo.init(user_name='tester', user_email='foo@test.bar', remove_old=True)
    co = repo.checkout(write=True)
    orig_lock = str(co._writer_lock)

    def mock_true(*args, **kwargs):
        return True

    co.metadata['hello'] = 'world'
    # try to release the writer lock with a process which has different uid
    co._writer_lock = 'lololol'
    with pytest.raises(RuntimeError):
        monkeypatch.setattr(co, '_WriterCheckout__acquire_writer_lock', mock_true)
        co.close()
    # replace, but rest of object is closed
    monkeypatch.setattr(co, '_writer_lock', orig_lock)
    co.close()
    repo._env._close_environments()


def test_force_release_writer_lock_works(managed_tmpdir):
    repo = Repository(path=managed_tmpdir, exists=False)
    repo.init(user_name='tester', user_email='foo@test.bar', remove_old=True)
    co = repo.checkout(write=True)
    co.metadata['hello'] = 'world'

    # try to release the writer lock with a process which has different uid
    with pytest.warns(ResourceWarning):
        repo.force_release_writer_lock()

    co._writer_lock == 'LOCK_AVAILABLE'
    co.close()
    # replace, but rest of object is closed
    repo._env._close_environments()


def test_get_ecosystem_details(managed_tmpdir):
    repo = Repository(path=managed_tmpdir, exists=False)
    repo.init(user_name='tester', user_email='foo@test.bar', remove_old=True)
    eco = repo._ecosystem_details()
    assert isinstance(eco, dict)
    assert 'host' in eco
    assert 'packages' in eco
    for package_name, version in eco['packages']:
        assert version is not None
    repo._env._close_environments()


def test_check_repository_version(written_repo):
    from hangar import __version__
    from pkg_resources import parse_version

    repo = written_repo
    assert repo.version == parse_version(__version__).base_version


def test_check_repository_software_version_startup(managed_tmpdir):
    from hangar import Repository, __version__
    from pkg_resources import parse_version

    repo = Repository(managed_tmpdir, exists=False)
    repo.init('test user', 'test@foo.bar', remove_old=True)
    repo._env._close_environments()

    nrepo = Repository(managed_tmpdir, exists=True)
    assert nrepo.initialized is True
    assert nrepo.version == parse_version(__version__).base_version
    nrepo._env._close_environments()


def test_check_repository_software_version_fails_on_older_repo(managed_tmpdir):
    from hangar import Repository
    from hangar.records.vcompat import set_repository_software_version

    repo = Repository(managed_tmpdir, exists=False)
    repo.init('test user', 'test@foo.bar', remove_old=True)
    # force writing of new software version. should trigger error on next read.
    set_repository_software_version(repo._env.branchenv, '0.2.0', overwrite=True)
    repo._env._close_environments()

    with pytest.raises(RuntimeError):
        Repository(managed_tmpdir, exists=True)


@pytest.mark.parametrize('futureVersion', ['1.0.0', '0.14.1', '0.15.0', '1.4.1'])
def test_check_repository_software_version_works_on_newer_hangar_version(managed_tmpdir, monkeypatch, futureVersion):
    from hangar import Repository

    repo = Repository(managed_tmpdir, exists=False)
    repo.init('test user', 'test@foo.bar', remove_old=True)
    old_version = repo.version
    # force writing of new software version. should trigger error on next read.
    repo._env._close_environments()

    import hangar
    monkeypatch.setattr(hangar, '__version__', futureVersion)
    nrepo = Repository(managed_tmpdir, exists=True)
    assert hangar.__version__ == futureVersion
    assert nrepo.version == old_version
    nrepo._env._close_environments()