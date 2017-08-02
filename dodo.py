import os
from os import path
from glob import glob
import json
import numpy as np
import pandas as pd
import h5py
import nested_h5py
import ratcave as rc


def read_motive_csv(fname):
    df = pd.read_csv(fname, skiprows=1, header=[0, 1, 3, 4],
                     index_col=[0, 1], tupleize_cols=True)
    df.index.names = ['Frame', 'Time']
    df.columns = [tuple(cols) if 'Unnamed' not in cols[3] else tuple([*cols[:-1] + (cols[-2],)]) for cols in df.columns]
#     df.col
    df.columns = pd.MultiIndex.from_tuples(df.columns, names=['DataSource', 'ObjectName', 'CoordinateType', 'Axis'])
    return df


def extract_motive_metadata(motive_csv_fname):
    with open(motive_csv_fname) as f:
        line = f.readline()

    cols = line.strip().split(',')
    session_metadata = {x: y for x, y in zip(cols[::2], cols[1::2])}

    # Attempt to coerce values to numeric data types, if possible
    for key, value in session_metadata.items():
        try:
            session_metadata[key] = float(value) if '.' in value else int(value)
        except ValueError:
            pass
    return session_metadata


def convert_motive_csv_to_hdf5(csv_fname, h5_fname):
    if not path.exists(path.split(path.abspath(h5_fname))[0]):
        os.makedirs(path.split(path.abspath(h5_fname))[0])
    df = read_motive_csv(csv_fname)
    #df = df.reset_index('Time')
    session_metadata = extract_motive_metadata(csv_fname)

    if session_metadata['Total Exported Frames'] != len(df):
        with open('log_csv_to_hdf5.txt', 'a') as f:
            f.write('Incomplete: {}, (csv: {} Frames of {} Motive Recorded Frames\r\n'.format(path.basename(csv_fname),
                len(df), session_metadata['Total Exported Frames']))

    nested_h5py.write_to_hdf5_group(h5_fname, df, '/', 
        compression='gzip', compression_opts=7)

    with h5py.File(h5_fname, 'r+') as f:
        f.attrs.update(session_metadata)


def add_orientation_dataset(h5_fname):
    with h5py.File(h5_fname, 'r+') as f:
        f.copy('/raw', '/preprocessed')
        for name, obj in nested_h5py.walk_h5py_path(f['/preprocessed/']):
            if not isinstance(obj, h5py.Dataset) or not 'Rotation' in name:
                continue

            rot_df = pd.DataFrame.from_records(obj.value).set_index('Frame')
            rot_df.columns = rot_df.columns.str.lower()

            oris, ori0 = [], rc.Camera().orientation0
            for _, row in rot_df.iterrows():
                oris.append(rc.RotationQuaternion(**row).rotate(ori0))

            odf = pd.DataFrame(oris, columns=['X', 'Y', 'Z'], index=rot_df.index)
            f.create_dataset(name=obj.parent.name + '/Orientation',
                data=odf.to_records(), compression='gzip', compression_opts=7)

    with open(path.join(path.dirname(h5_fname), 'ori_added.txt'), 'w'):
        pass

    return None


def unrotate_objects(h5_fname, group='/preprocessed/Rigid Body', source_object_name='Arena', add_rotation=10.5, mean_center=True, index_cols=1):
    """
    Un-rotate the objects in an hdf5 group by either a set rotationa mount, another object's rotation, or both.

    Arguments:
       -h5_fname (str): filename of hdf5 file to read in.
       -group (str): hdf5 group directory where all objects can be found
       -source_object_name (str): object name to use as un-rotation parent.
       -add_rotation (float): additional amount (degrees) to rotate by.
       -mean_center (bool): if the position should also be moved by the source_object's position.  
       
    """
    # Get rotation
    source_obj = nested_h5py.read_from_h5_group(h5_fname, path.join(group, source_object_name), index_cols=index_cols)
    mean_rot = source_obj.Rotation.mean()
    mean_rot.index = mean_rot.index.str.lower()
    rot_mat = rc.RotationQuaternion(**mean_rot).to_matrix()
    # assert np.isclose(source_obj.Orientation.mean().values @ rot_mat[:-1, :-1], [0., 0., -1.]).all()

    # Apply rotation
    with h5py.File(h5_fname, 'r+') as f:
        bodies = f[group]
        body_paths = [bodies[body].name for body in bodies]
        
        
    manrot = rc.RotationEulerDegrees(x=0, y=add_rotation, z=0).to_matrix()
    
    for body in body_paths:
        obj = nested_h5py.read_from_h5_group(h5_fname, path.join(group, body), index_cols=index_cols)
        if mean_center:
            obj.Position -= source_obj.Position.mean()
        obj.Orientation @= rot_mat[:-1, :-1]
        obj.Orientation @= manrot[:-1, :-1]
        
        nested_h5py.write_to_hdf5_group(h5_fname, obj, body + '/',
                                        mode='r+', overwrite=True)

    with open(path.join(path.dirname(h5_fname), 'unrotated.txt'), 'w'):
        pass
        


event_log_dir = '/home/nickdg/theta_storage/data/VR_Experiments_Round_2/logs/event_logs/'
settings_log_dir = '/home/nickdg/theta_storage/data/VR_Experiments_Round_2/logs/settings_logs/'


def add_event_log(csv_fname, h5_fname):
    log_fname = path.join(event_log_dir, path.basename(csv_fname))

    if not path.exists(log_fname):
        # Attempt to match name
        for backidx in range(1, 15):
            fname_part = log_fname[:-backidx]
            matches = glob(fname_part + '*')
            if len(matches) == 1:
                log_fname = matches[0]
                break
            if len(matches) > 1:
                print('No matching log found for {}'.format(log_fname))
                return
        else:
            print('No matching log found for {}'.format(log_fname))
            return
    events = pd.read_csv(log_fname, sep=';',)# parse_dates=[0], infer_datetime_format=True)
    events.columns = events.columns.str.lstrip()
    events['Event'] = events.Event.str.lstrip()
    events['EventArguments'] = events.EventArguments.str.lstrip()

    times = pd.read_hdf(h5_fname, '/raw/Rigid Body/Rat/Position').set_index('Frame')['Time']
    event_frames = np.searchsorted(times.values.flatten(), events['MotiveExpTimeSecs'])
    events['Frame'] = event_frames
    events['Time'] = times.loc[event_frames].values

    phase_frames = events[events.Event.str.match('set_')].reset_index().Frame.values

    events.set_index(['Frame', 'Time'], inplace=True)
    event_names = events.Event.values.astype('S')
    del events['Event']
    event_arguments = events.EventArguments.values.astype('S')
    del events['EventArguments']
    del events['DateTime']
    with h5py.File(h5_fname, 'r+') as f:
        f.create_dataset('/events/eventlog', data=events.to_records(),
            compression='gzip', compression_opts=7)
        f.create_dataset('/events/eventNames', data=event_names)
        f.create_dataset('/events/eventArguments', data=event_arguments)
        if len(phase_frames) > 0:
            f.create_dataset('/events/phaseStartFrameNum', data=phase_frames)

    with open(path.join(path.dirname(h5_fname), 'event_log_added.txt'), 'w'):
        pass

    return None


def add_settings_log(json_fname, h5_fname):
    """
    Writes a settings log to the hdf5 file as root user attributes, using csv data.

    Arguments:
        -json_fname (str): json filename to read for settings info
        -h5_fname (str): hdf5 filename to write to.
    """
    log_fname = path.join(settings_log_dir, path.basename(json_fname))

    if not path.exists(log_fname):
        # Attempt to match name
        for backidx in range(1, 15):
            fname_part = log_fname[:-backidx]
            matches = glob(fname_part + '*')
            if len(matches) == 1:
                log_fname = matches[0]
                break
            if len(matches) > 1:
                print('No matching log found for {}'.format(log_fname))
                return
        else:
            print('No matching log found for {}'.format(log_fname))
            return

    with open(log_fname) as f:
        sess_data = json.load(f)

    for key, value in sess_data.items():
        if type(value) == bool:
            sess_data[key] = int(value)

    with h5py.File(h5_fname, 'r+') as f:
        f.attrs.update(sess_data)

    with open(path.join(path.dirname(h5_fname), 'settings_log_added.txt'), 'w'):
        pass

    return None

basedir = '/home/nickdg/theta_storage/data/VR_Experiments_Round_2/Converted Motive Files'


csv_fnames = glob(basedir + '/**/*.csv', recursive=True)
new_basedir = path.join(path.commonpath(csv_fnames), '..', 'processed_data')
h5_fnames = [path.join(new_basedir, path.basename(path.splitext(name)[0]), path.basename(path.splitext(name)[0] + '.h5')) for name in csv_fnames]


def task_preprocess_all_data():
    for csv_fname, h5_fname in zip(csv_fnames[:60], h5_fnames):
        if 'test' in csv_fname.lower():
            continue
        if 'habit' in csv_fname.lower():
            continue
        convert_task = {
            'actions': [(convert_motive_csv_to_hdf5, (csv_fname, h5_fname))],
            'targets': [h5_fname],
            'file_dep': [csv_fname],
            'name': 'convert_csv_to_h5: {}'.format(path.basename(h5_fname)),
        }
        yield convert_task

        event_task = {
            'actions': [(add_event_log, (csv_fname, h5_fname,))],
            # 'targets': [path.join(path.dirname(h5_fname), 'event_log_added.txt')],
            'task_dep': [convert_task['name']],
            'file_dep': [h5_fname],
            'name': 'add_event_log: {}'.format(path.basename(h5_fname)),
            'verbosity': 2,
        }
        yield event_task

        settings_task = {
            'actions': [(add_settings_log, (csv_fname, h5_fname,))],
            'targets': [path.join(path.dirname(h5_fname), 'settings_log_added.txt')],
            'task_dep': [event_task['name']],
            'file_dep': [h5_fname],
            'name': 'add_settings_log: {}'.format(path.basename(h5_fname)),
            'verbosity': 2,

        }
        yield settings_task


        # ori_task = {
        #     'actions': [(add_orientation_dataset, (h5_fname,))],
        #     'targets': [path.join(path.dirname(h5_fname), 'ori_added.txt')],
        #     'file_dep': [h5_fname],
        #     'task_dep': [convert_task['name']],
        #     'name': 'add_orientation: {}'.format(path.basename(h5_fname)),
        # }
        # yield ori_task

        # rotate_task = {
        #     'actions': [(unrotate_objects, (h5_fname,))],
        #     'targets': [path.join(path.dirname(h5_fname), 'unrotated.txt')],
        #     'file_dep': [h5_fname],
        #     'task_dep': [ori_task['name']],
        #     'name': 'unrotate: {}'.format(path.basename(h5_fname)),
        # }
        # yield rotate_task


    

if __name__ == '__main__':
    import doit
    doit.run(globals())